#!/usr/bin/env python3
"""hex-events — CLI for querying and debugging the event system.

Usage:
  hex-events status                  # Daemon running? Last poll? Recipe count?
  hex-events history [--since 1]     # Event timeline (hours)
  hex-events inspect <event-id>      # Full trace for one event
  hex-events recipes                 # List loaded recipes
  hex-events test <recipe-file>      # Dry-run a recipe with a mock event
  hex-events validate                # Static policy graph validation
  hex-events graph [--observed]      # Show event dependency graph
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import EventsDB
from recipe import load_recipes

BASE_DIR = os.path.expanduser("~/.hex-events")
DB_PATH = os.path.join(BASE_DIR, "events.db")
RECIPES_DIR = os.path.join(BASE_DIR, "recipes")
POLICIES_DIR = os.path.join(BASE_DIR, "policies")

def cmd_status(args):
    # Check if daemon is running
    result = subprocess.run(["pgrep", "-f", "hex_eventd"], capture_output=True, text=True)
    running = result.returncode == 0
    pid = result.stdout.strip().split("\n")[0] if running else None

    recipes = load_recipes(RECIPES_DIR) if os.path.isdir(RECIPES_DIR) else []
    db = EventsDB(DB_PATH)
    unprocessed = len(db.get_unprocessed())
    total = len(db.history(limit=1000))
    db.close()

    print(f"Daemon:      {'running (pid {})'.format(pid) if running else 'NOT RUNNING'}")
    print(f"Recipes:     {len(recipes)} loaded")
    print(f"Unprocessed: {unprocessed} events")
    print(f"Total (7d):  {total} events")

def cmd_history(args):
    db = EventsDB(DB_PATH)
    events = db.history(limit=50, since_hours=args.since)
    db.close()
    if not events:
        print("No events found.")
        return
    for e in reversed(events):  # oldest first
        processed = "✓" if e["processed_at"] else "·"
        recipe = e["recipe"] or ""
        print(f"  {processed} [{e['id']:4d}] {e['created_at']}  {e['event_type']:30s}  {e['source']:15s}  {recipe}")

def cmd_inspect(args):
    db = EventsDB(DB_PATH)
    rows = db.conn.execute("SELECT * FROM events WHERE id = ?", (args.event_id,)).fetchall()
    if not rows:
        print(f"Event {args.event_id} not found.")
        db.close()
        return
    event = dict(rows[0])
    print(f"Event #{event['id']}")
    print(f"  Type:      {event['event_type']}")
    print(f"  Source:    {event['source']}")
    print(f"  Created:   {event['created_at']}")
    print(f"  Processed: {event['processed_at'] or 'not yet'}")
    print(f"  Recipe:    {event['recipe'] or 'none matched'}")
    print(f"  Payload:   {event['payload']}")
    logs = db.get_action_logs(args.event_id)
    if logs:
        print(f"  Actions:")
        for log in logs:
            print(f"    [{log['status']}] {log['action_type']} — {log['action_detail'][:80]}")
            if log["error_message"]:
                print(f"           {log['error_message'][:100]}")
    db.close()

def cmd_recipes(args):
    recipes = load_recipes(RECIPES_DIR)
    if not recipes:
        print("No recipes loaded.")
        return
    for r in recipes:
        print(f"  {r.name:25s}  trigger={r.trigger_event:25s}  actions={len(r.actions)}  conditions={len(r.conditions)}")

def cmd_test(args):
    from recipe import Recipe
    import yaml
    try:
        with open(args.recipe_file) as f:
            data = yaml.safe_load(f)
        recipe = Recipe.from_dict(data, source_file=args.recipe_file)
        print(f"Recipe: {recipe.name}")
        print(f"  Trigger: {recipe.trigger_event}")
        print(f"  Conditions: {len(recipe.conditions)}")
        print(f"  Actions: {len(recipe.actions)}")
        print(f"  [DRY RUN] Would fire {len(recipe.actions)} action(s) on matching event.")
    except FileNotFoundError:
        print(f"Recipe file not found: {args.recipe_file}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Invalid YAML in {args.recipe_file}: {e}")
        sys.exit(1)
    except (KeyError, TypeError) as e:
        print(f"Invalid recipe structure: {e}")
        sys.exit(1)

def _load_all_policies():
    """Load policies from policies/ dir (preferred) or recipes/ dir (fallback)."""
    from policy import load_policies
    policies_dir = POLICIES_DIR if os.path.isdir(POLICIES_DIR) else RECIPES_DIR
    if not os.path.isdir(policies_dir):
        return [], policies_dir
    return load_policies(policies_dir), policies_dir


def cmd_validate(args):
    from validator import build_static_graph, validate_graph, load_adapter_events

    policies, policies_dir = _load_all_policies()
    adapter_events = load_adapter_events()

    print(f"Loaded {len(policies)} policies from {policies_dir}")
    print(f"Adapter events: {sorted(adapter_events) or '(none)'}")
    print()

    graph = build_static_graph(policies, adapter_events=adapter_events)
    result = validate_graph(graph)

    if result["unsatisfied"]:
        print(f"[ERROR] {len(result['unsatisfied'])} unsatisfied require(s):")
        for u in result["unsatisfied"]:
            consumers = ", ".join(u["required_by"])
            print(f"  x {u['event']}  (required by: {consumers})")
    else:
        print("[OK] All requires satisfied.")

    print()

    if result["orphan_provides"]:
        print(f"[WARN] {len(result['orphan_provides'])} orphan provide(s) (no consumer):")
        for o in result["orphan_provides"]:
            providers = ", ".join(o["provided_by"])
            print(f"  ~ {o['event']}  (provided by: {providers})")
    else:
        print("[OK] No orphan provides.")

    print()

    if result["cycles"]:
        print(f"[ERROR] {len(result['cycles'])} cycle(s) detected:")
        for cycle in result["cycles"]:
            print(f"  -> {' -> '.join(cycle)}")
    else:
        print("[OK] No cycles detected.")

    print()
    status = "VALID" if result["valid"] else "INVALID"
    print(f"Validation: {status}")

    if not result["valid"]:
        sys.exit(1)


def cmd_graph(args):
    from validator import (
        build_static_graph, load_adapter_events,
        get_observed_events, compare_graphs,
    )

    policies, _policies_dir = _load_all_policies()
    adapter_events = load_adapter_events()
    graph = build_static_graph(policies, adapter_events=adapter_events)

    if args.observed:
        db = EventsDB(DB_PATH)
        observed = get_observed_events(db, days=7)
        db.close()

        print("=== Observed Event Graph (last 7 days) ===")
        print()
        if not observed["event_counts"]:
            print("No events observed in last 7 days.")
            return

        for evt, count in sorted(observed["event_counts"].items(),
                                  key=lambda x: -x[1]):
            consumers = observed["policy_triggers"].get(evt, [])
            consumer_str = ""
            if consumers:
                names = ", ".join(f"{p['policy']}x{p['count']}" for p in consumers)
                consumer_str = f"  -> [{names}]"
            print(f"  {evt:40s} {count:5d} events{consumer_str}")

        print()
        cmp = compare_graphs(graph, observed)
        if cmp["in_static_only"]:
            print(f"In static only (never observed): {', '.join(cmp['in_static_only'])}")
        if cmp["in_observed_only"]:
            print(f"In observed only (not in static): {', '.join(cmp['in_observed_only'])}")
        if cmp["in_both"]:
            print(f"In both: {', '.join(cmp['in_both'])}")

    else:
        print("=== Static Event Dependency Graph ===")
        print()
        all_events = sorted(
            set(graph["provided_by"].keys()) | set(graph["required_by"].keys())
        )
        if not all_events:
            print("No events in static graph.")
            return

        for evt in all_events:
            providers = graph["provided_by"].get(evt, [])
            consumers = graph["required_by"].get(evt, [])
            prov_str = ", ".join(providers) if providers else "(external)"
            cons_str = ", ".join(consumers) if consumers else "(terminal)"
            print(f"  {evt}")
            print(f"    provided by: {prov_str}")
            print(f"    consumed by: {cons_str}")
        print()
        print(f"Total: {len(all_events)} events, {len(policies)} policies, "
              f"{len(adapter_events)} adapter events")


def main():
    parser = argparse.ArgumentParser(description="hex-events CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show daemon and system status")

    hist = sub.add_parser("history", help="Show event timeline")
    hist.add_argument("--since", type=int, default=None, help="Hours to look back")

    insp = sub.add_parser("inspect", help="Inspect a specific event")
    insp.add_argument("event_id", type=int, help="Event ID")

    sub.add_parser("recipes", help="List loaded recipes")

    test = sub.add_parser("test", help="Dry-run a recipe file")
    test.add_argument("recipe_file", help="Path to recipe YAML")

    sub.add_parser("validate", help="Validate static policy event graph")

    graph_p = sub.add_parser("graph", help="Show event dependency graph")
    graph_p.add_argument("--observed", action="store_true",
                         help="Show observed graph from DB (last 7 days)")

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "recipes":
        cmd_recipes(args)
    elif args.command == "test":
        cmd_test(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "graph":
        cmd_graph(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
