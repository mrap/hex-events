#!/usr/bin/env python3
"""hex-eventd — persistent daemon for hex-events."""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import EventsDB
from recipe import Recipe, load_recipes
from policy import load_policies, check_rate_limit, record_fire
from conditions import evaluate_conditions
from actions import get_action_handler
from adapters.scheduler import SchedulerAdapter

BASE_DIR = os.path.expanduser("~/.hex-events")
DB_PATH = os.path.join(BASE_DIR, "events.db")
RECIPES_DIR = os.path.join(BASE_DIR, "recipes")
POLICIES_DIR = os.path.join(BASE_DIR, "policies")
SCHEDULER_CONFIG = os.path.join(BASE_DIR, "adapters", "scheduler.yaml")
POLL_INTERVAL = 2  # seconds
JANITOR_INTERVAL = 3600  # run janitor every hour
SCHEDULER_RELOAD_INTERVAL = 60  # reload scheduler config every minute

log = logging.getLogger("hex-events")

def drain_deferred(db: EventsDB):
    """Drain due deferred events into the main events table.

    Dual-write safety: delete from deferred_events FIRST, then insert into events.
    This means a crash between the two steps loses the event (acceptable: lost > doubled).
    """
    due = db.get_due_deferred()
    for row in due:
        db.delete_deferred(row["id"])
        db.insert_event(row["event_type"], row["payload"], row["source"])


def match_recipes(recipes: list[Recipe], event_type: str) -> list[Recipe]:
    return [r for r in recipes if r.matches_event_type(event_type)]


def run_action_with_retry(action, event_id: int, recipe_name: str, payload: dict,
                          db: EventsDB, handler=None, sleep_fn=None):
    """Run an action with exponential backoff retry.

    Retries up to action.params.get('retries', 3) times on failure.
    Backoff: 1s, 2s, 4s, ...

    Args:
        handler: Override the action handler (for testing). If None, looks up via registry.
        sleep_fn: Override time.sleep (for testing).
    Returns:
        The final result dict from the handler.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep

    max_retries = action.params.get("retries", 3)

    if handler is None:
        handler = get_action_handler(action.type)
    if not handler:
        msg = f"Unknown action type: {action.type}"
        db.log_action(event_id, recipe_name, action.type,
                      json.dumps(action.params), "error", msg)
        return {"status": "error", "output": msg}

    backoff = 1
    for attempt in range(max_retries + 1):
        result = handler.run(action.params, event_payload=payload, db=db)
        status = result.get("status", "error")

        if status != "error":
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), status,
                          result.get("output", ""))
            return result

        # Action failed
        if attempt < max_retries:
            retry_label = f"retry_{attempt + 1}"
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), retry_label,
                          f"Retry {attempt + 1}/{max_retries}: {result.get('output', '')}")
            sleep_fn(backoff)
            backoff *= 2
        else:
            # Final failure after all retries exhausted
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), "error",
                          f"Permanently failed after {max_retries} retries: {result.get('output', '')}")

    return result


def process_event(event: dict, recipes: list[Recipe], db: EventsDB):
    event_type = event["event_type"]
    event_id = event["id"]
    try:
        payload = json.loads(event["payload"])
    except json.JSONDecodeError as e:
        log.error("Event %s has malformed JSON payload %r: %s", event_id, event["payload"], e)
        db.mark_processed(event_id, None)
        return

    matched = match_recipes(recipes, event_type)
    matched_names = []

    for recipe in matched:
        if not evaluate_conditions(recipe.conditions, payload, db=db):
            continue
        matched_names.append(recipe.name)
        for action in recipe.actions:
            run_action_with_retry(action, event_id, recipe.name, payload, db)

    recipe_column = ",".join(matched_names) if matched_names else None
    db.mark_processed(event_id, recipe_column)

def _process_event_policies(event: dict, policies: list, db: "EventsDB"):
    """Process an event against Policy objects with per-policy rate limiting.

    Iterates each policy, checks its rate limit, then evaluates matching rules.
    Records a fire timestamp per policy only when at least one rule fires.
    """
    event_type = event["event_type"]
    event_id = event["id"]
    try:
        payload = json.loads(event["payload"])
    except json.JSONDecodeError as e:
        log.error("Event %s has malformed JSON payload %r: %s", event_id, event["payload"], e)
        db.mark_processed(event_id, None)
        return

    matched_names = []
    for policy in policies:
        matching_rules = [r for r in policy.rules if r.matches_event_type(event_type)]
        if not matching_rules:
            continue
        if not check_rate_limit(policy):
            log.info("Rate limited: policy %s skipped for event %s", policy.name, event_type)
            continue
        fired = False
        for rule in matching_rules:
            if not evaluate_conditions(rule.conditions, payload, db=db):
                continue
            matched_names.append(rule.name)
            fired = True
            for action in rule.actions:
                run_action_with_retry(action, event_id, rule.name, payload, db)
        if fired:
            record_fire(policy)

    recipe_column = ",".join(matched_names) if matched_names else None
    db.mark_processed(event_id, recipe_column)


def run_daemon():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("hex-eventd starting (pid=%d)", os.getpid())

    db = EventsDB(DB_PATH)

    scheduler = SchedulerAdapter(config_path=SCHEDULER_CONFIG)
    try:
        caught_up = scheduler.startup_catchup(db)
        if caught_up:
            log.info("Scheduler catchup: emitted %s", caught_up)
    except Exception as e:
        log.error("Scheduler startup catchup failed: %s", e)

    running = True
    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_janitor = 0
    last_recipe_load = 0
    last_scheduler_reload = 0
    recipes = []
    _policies = []

    while running:
        now = time.time()

        # Reload policies/recipes every 10 seconds (hot-reload).
        # Prefer policies/ dir if it exists; fall back to recipes/ for backwards compat.
        if now - last_recipe_load > 10:
            if os.path.isdir(POLICIES_DIR):
                _policies = load_policies(POLICIES_DIR)
                recipes = []  # unused in policies mode
            else:
                _policies = []
                os.makedirs(RECIPES_DIR, exist_ok=True)
                recipes = load_recipes(RECIPES_DIR)
            last_recipe_load = now

        # Reload scheduler config periodically
        if now - last_scheduler_reload > SCHEDULER_RELOAD_INTERVAL:
            scheduler.reload()
            last_scheduler_reload = now

        # Tick the scheduler — emits timer events for due cron windows
        try:
            scheduler.tick(db, now=datetime.utcnow())
        except Exception as e:
            log.error("Scheduler tick error: %s", e)

        # Drain deferred events whose fire_at has passed
        try:
            drain_deferred(db)
        except Exception as e:
            log.error("Error draining deferred events: %s", e)

        # Process unprocessed events
        try:
            events = db.get_unprocessed()
            for event in events:
                if _policies:
                    _process_event_policies(event, _policies, db)
                else:
                    process_event(event, recipes, db)
        except Exception as e:
            log.error("Error processing events: %s", e)

        # Janitor
        if now - last_janitor > JANITOR_INTERVAL:
            try:
                deleted = db.janitor(days=7)
                if deleted > 0:
                    log.info("Janitor: deleted %d old events", deleted)
            except Exception as e:
                log.error("Janitor error: %s", e)
            last_janitor = now

        time.sleep(POLL_INTERVAL)

    db.close()
    log.info("hex-eventd stopped")

if __name__ == "__main__":
    run_daemon()
