#!/usr/bin/env python3
"""hex-eventd — persistent daemon for hex-events."""
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import EventsDB, parse_duration
from recipe import Recipe
from policy import load_policies, check_rate_limit, record_fire
from conditions import evaluate_conditions, evaluate_conditions_with_details
from actions import get_action_handler
from adapters.scheduler import SchedulerAdapter
from policy_validator import validate_policy

BASE_DIR = os.path.expanduser("~/.hex-events")
DB_PATH = os.path.join(BASE_DIR, "events.db")
PID_FILE = os.path.join(BASE_DIR, "hex_eventd.pid")
LOG_FILE = os.path.join(BASE_DIR, "daemon.log")
POLICIES_DIR = os.path.join(BASE_DIR, "policies")
SCHEDULER_CONFIG = os.path.join(BASE_DIR, "adapters", "scheduler.yaml")
POLL_INTERVAL = 2  # seconds
JANITOR_INTERVAL = 3600  # run janitor every hour
SCHEDULER_RELOAD_INTERVAL = 60  # reload scheduler config every minute
HEARTBEAT_INTERVAL = 300  # heartbeat log every 5 minutes

log = logging.getLogger("hex-events")


def _acquire_singleton_lock():
    """Acquire an exclusive lock on PID_FILE to prevent multiple daemon instances.

    Uses fcntl.flock() which auto-releases on process death (no stale PID files).
    Returns the open file handle — caller must keep it open for the lock to hold.
    Exits with code 0 if another instance already holds the lock.
    """
    os.makedirs(BASE_DIR, exist_ok=True)
    fh = open(PID_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("hex-eventd: another instance is already running, exiting", file=sys.stderr)
        fh.close()
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def drain_deferred(db: EventsDB):
    """Drain due deferred events into the main events table.

    Dual-write safety: delete from deferred_events FIRST, then insert into events.
    This means a crash between the two steps loses the event (acceptable: lost > doubled).
    """
    due = db.get_due_deferred()
    for row in due:
        db.delete_deferred(row["id"])
        db.insert_event(row["event_type"], row["payload"], row["source"])


def match_policies(policies: list[Recipe], event_type: str) -> list[Recipe]:
    return [r for r in policies if r.matches_event_type(event_type)]


def run_action_with_retry(action, event_id: int, recipe_name: str, payload: dict,
                          db: EventsDB, handler=None, sleep_fn=None,
                          workflow_context=None):
    """Run an action with exponential backoff retry.

    Retries up to action.params.get('retries', 3) times on failure.
    Backoff: 1s, 2s, 4s, ...

    Args:
        handler: Override the action handler (for testing). If None, looks up via registry.
        sleep_fn: Override time.sleep (for testing).
        workflow_context: Optional dict {"name": ..., "config": {...}} for Jinja2 templates.
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
        result = handler.run(action.params, event_payload=payload, db=db,
                             workflow_context=workflow_context)
        status = result.get("status", "error")

        if status != "error":
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), status,
                          result.get("output", ""))
            return result

        # Action failed
        if attempt < max_retries:
            retry_label = f"retry_{attempt + 1}"
            err_detail = (result.get("output") or "")[:500]
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), retry_label,
                          f"Retry {attempt + 1}/{max_retries}: {err_detail}")
            sleep_fn(backoff)
            backoff *= 2
        else:
            # Final failure after all retries exhausted
            err_detail = (result.get("output") or "")[:500]
            db.log_action(event_id, recipe_name, action.type,
                          json.dumps(action.params), "error",
                          f"Permanently failed after {max_retries} retries: {err_detail}")

    return result


def _disable_policy_file(path: str):
    """Rewrite a policy YAML file with enabled: false."""
    with open(path) as f:
        data = yaml.safe_load(f)
    data["enabled"] = False
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    os.rename(tmp_path, path)


def _handle_policy_lifecycle(policy, db: EventsDB):
    """Handle oneshot/max_fires lifecycle after successful policy fire."""
    lc = getattr(policy, "lifecycle", "persistent")
    max_fires = getattr(policy, "max_fires", None)
    path = policy.source_file

    if lc == "oneshot-delete":
        if path and os.path.exists(path):
            os.remove(path)
            log.info("Policy %s fired (oneshot) and self-destructed", policy.name)
    elif lc == "oneshot-disable":
        if path and os.path.exists(path):
            _disable_policy_file(path)
            log.info("Policy %s fired (oneshot) and disabled", policy.name)

    if max_fires is not None:
        fires_so_far = db.count_policy_fires(policy.name)
        total = fires_so_far + 1  # +1 for the current fire (not yet logged)
        if total >= max_fires:
            if path and os.path.exists(path):
                _disable_policy_file(path)
                log.info("Policy %s reached max_fires=%d and was auto-disabled",
                         policy.name, max_fires)


def process_event(event: dict, policies: list[Recipe], db: EventsDB):
    event_type = event["event_type"]
    event_id = event["id"]
    try:
        payload = json.loads(event["payload"])
    except json.JSONDecodeError as e:
        log.error("Event %s has malformed JSON payload %r: %s", event_id, event["payload"], e)
        db.mark_processed(event_id, None)
        return 0

    matched = match_policies(policies, event_type)
    matched_names = []

    for recipe in matched:
        if not evaluate_conditions(recipe.conditions, payload, db=db):
            continue
        matched_names.append(recipe.name)
        for action in recipe.actions:
            run_action_with_retry(action, event_id, recipe.name, payload, db,
                                  workflow_context=None)

    recipe_column = ",".join(matched_names) if matched_names else None
    db.mark_processed(event_id, recipe_column)

def _process_event_policies(event: dict, policies: list, db: "EventsDB") -> int:
    """Process an event against Policy objects with per-policy rate limiting.

    Iterates each policy, checks its rate limit, then evaluates matching rules.
    Records a fire timestamp per policy only when at least one rule fires.

    Returns the number of actions dispatched.
    """
    event_type = event["event_type"]
    event_id = event["id"]
    try:
        payload = json.loads(event["payload"])
    except json.JSONDecodeError as e:
        log.error("Event %s has malformed JSON payload %r: %s", event_id, event["payload"], e)
        db.mark_processed(event_id, None)
        return 0

    matched_names = []
    eval_rows = []
    now_ts = datetime.utcnow().isoformat()
    actions_dispatched = 0

    for policy in policies:
        matching_rules = [r for r in policy.rules if r.matches_event_type(event_type)]
        if not matching_rules:
            continue
        if not check_rate_limit(policy):
            rl = policy.rate_limit or {}
            max_fires = rl.get("max_fires", 0)
            window_str = str(rl.get("window", "1h"))
            window_secs = parse_duration(window_str)
            cutoff = time.time() - window_secs
            fires_in_window = len([t for t in policy.last_fires if t >= cutoff])
            rule_names = ",".join(r.name for r in matching_rules)
            detail = json.dumps({
                "policy": policy.name,
                "rule": rule_names,
                "fires_in_window": fires_in_window,
                "max_fires": max_fires,
                "window": window_str,
            })
            err_msg = f"Rate limited: {fires_in_window}/{max_fires} fires in {window_str}"
            log.warning("Rate limited: policy %s skipped for event %s (%d/%d fires in %s)",
                        policy.name, event_type, fires_in_window, max_fires, window_str)
            db.log_action(event_id, policy.name, "rate_limited", detail, "suppressed", err_msg)
            for rule in matching_rules:
                eval_rows.append({
                    "event_id": event_id,
                    "policy_name": policy.name,
                    "rule_name": rule.name,
                    "matched": 1,
                    "conditions_passed": None,
                    "condition_details": None,
                    "rate_limited": 1,
                    "action_taken": 0,
                    "evaluated_at": now_ts,
                    "workflow": policy.workflow,
                })
            continue
        fired = False
        all_actions_succeeded = True
        for rule in matching_rules:
            conditions_passed, cond_details = evaluate_conditions_with_details(
                rule.conditions, payload, db=db
            )
            action_taken = 0
            if conditions_passed:
                matched_names.append(rule.name)
                fired = True
                action_taken = 1
                wf_ctx = None
                if policy.workflow:
                    wf_ctx = {"name": policy.workflow,
                              "config": policy.workflow_config}
                for action in rule.actions:
                    result = run_action_with_retry(action, event_id, rule.name,
                                          payload, db,
                                          workflow_context=wf_ctx)
                    actions_dispatched += 1
                    if result.get("status") == "error":
                        all_actions_succeeded = False
            eval_rows.append({
                "event_id": event_id,
                "policy_name": policy.name,
                "rule_name": rule.name,
                "matched": 1,
                "conditions_passed": 1 if conditions_passed else 0,
                "condition_details": json.dumps(cond_details) if cond_details else None,
                "rate_limited": 0,
                "action_taken": action_taken,
                "evaluated_at": now_ts,
                "workflow": policy.workflow,
            })
        if fired:
            record_fire(policy)
            if all_actions_succeeded:
                _handle_policy_lifecycle(policy, db)

    if eval_rows:
        try:
            db.log_policy_evals(eval_rows)
        except Exception as e:
            log.error("Failed to log policy evals for event %s: %s", event_id, e)

    recipe_column = ",".join(matched_names) if matched_names else None
    db.mark_processed(event_id, recipe_column)
    return actions_dispatched


def _load_policies_validated(policies_dir: str) -> list:
    """Load policies from directory, validate each, skip invalid ones.

    Logs errors to both daemon.log and stderr. Prints a startup summary.
    """
    skipped = [0]

    def on_invalid(fpath, errors):
        for err in errors:
            msg = f"[POLICY VALIDATION ERROR] {err}"
            log.error(msg)
            print(msg, file=sys.stderr)
        skipped[0] += 1

    policies = load_policies(policies_dir, on_invalid=on_invalid)
    n_valid = len(policies)
    n_skipped = skipped[0]
    summary = f"Loaded {n_valid} policies ({n_skipped} skipped due to validation errors)"
    log.info(summary)
    if n_skipped > 0:
        print(summary, file=sys.stderr)
    return policies


def _setup_logging():
    """Configure logging to write directly to daemon.log via RotatingFileHandler.

    This works regardless of how the daemon is launched (launchd or manual).
    """
    os.makedirs(BASE_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)


def run_daemon():
    pid_fh = _acquire_singleton_lock()
    _setup_logging()
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
    last_heartbeat = time.time()
    _hb_events = 0
    _hb_actions = 0
    _policies = []

    while running:
        now = time.time()

        # Reload policies every 10 seconds (hot-reload), with validation.
        if now - last_recipe_load > 10:
            _policies = _load_policies_validated(POLICIES_DIR)
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
            _hb_events += len(events)
            for event in events:
                _hb_actions += _process_event_policies(event, _policies, db)
        except Exception as e:
            log.error("Error processing events: %s", e)

        # Heartbeat
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            log.debug(
                "heartbeat: %d events processed, %d actions fired since last heartbeat",
                _hb_events, _hb_actions,
            )
            _hb_events = 0
            _hb_actions = 0
            last_heartbeat = now

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
    pid_fh.close()
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    log.info("hex-eventd stopped")

if __name__ == "__main__":
    run_daemon()
