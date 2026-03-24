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
  hex-events trace <event-id>        # Policy evaluation trace for an event
  hex-events trace --policy <name> [--since <hours>]  # Policy trace over time
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
    event_ids = [e["id"] for e in events]
    rate_limited_map = db.get_rate_limited_by_event(event_ids)
    db.close()
    if not events:
        print("No events found.")
        return
    for e in reversed(events):  # oldest first
        eid = e["id"]
        if eid in rate_limited_map:
            marker = "⊘"
            policy = rate_limited_map[eid]
            suffix = f"(rate limited: {policy})"
        elif e["processed_at"]:
            marker = "✓"
            suffix = e["recipe"] or ""
        else:
            marker = "·"
            suffix = e["recipe"] or ""
        print(f"  {marker} [{eid:4d}] {e['created_at']}  {e['event_type']:30s}  {e['source']:15s}  {suffix}")

def _format_condition_detail(idx: int, detail: dict) -> str:
    """Format a single condition detail for inspect output."""
    field = detail.get("field", "?")
    op = detail.get("op", "?")
    expected = detail.get("expected", "?")
    passed = detail.get("passed")

    if passed == "not_evaluated":
        return f"    Condition {idx}: {field} {op} {expected} (not evaluated, short-circuited)"

    actual = detail.get("actual")
    marker = "✓" if passed else "✗"
    return f"    Condition {idx}: {field} {op} {expected} → actual: {actual} {marker}"


def cmd_inspect(args):
    db = EventsDB(DB_PATH)
    rows = db.conn.execute("SELECT * FROM events WHERE id = ?", (args.event_id,)).fetchall()
    if not rows:
        print(f"Event {args.event_id} not found.")
        db.close()
        return
    event = dict(rows[0])
    print(f"Event #{event['id']}: {event['event_type']}")
    print(f"  Source:    {event['source']}")
    print(f"  Created:   {event['created_at']}")
    print(f"  Processed: {event['processed_at'] or 'not yet'}")
    print(f"  Recipe:    {event['recipe'] or 'none matched'}")
    print(f"  Payload:   {event['payload']}")

    # Show policy evaluation details
    policy_evals = db.get_policy_evals(args.event_id)
    if policy_evals:
        print("  Policy Evaluations:")
        for eval_row in policy_evals:
            policy_name = eval_row["policy_name"]
            rule_name = eval_row["rule_name"]
            rate_limited = eval_row.get("rate_limited")
            action_taken = eval_row.get("action_taken")
            print(f"    Policy: {policy_name}")
            print(f"      Rule: {rule_name}")
            if rate_limited:
                print(f"      (rate limited)")
            else:
                cond_json = eval_row.get("condition_details")
                if cond_json:
                    try:
                        cond_details = json.loads(cond_json)
                        for i, detail in enumerate(cond_details, 1):
                            print(_format_condition_detail(i, detail))
                    except (json.JSONDecodeError, TypeError):
                        pass
                status = "success" if action_taken else "skipped"
                print(f"      Actions: {status}")

    logs = db.get_action_logs(args.event_id)
    if logs:
        print("  Action Log:")
        for log in logs:
            print(f"    [{log['status']}] {log['action_type']} — {log['action_detail'][:80]}")
            if log["error_message"]:
                print(f"           {log['error_message'][:100]}")
    db.close()

def _format_trace_row(event_type: str, row: dict, action_logs: list) -> str:
    """Format a single policy_eval_log row for the trace output."""
    rate_limited = row.get("rate_limited")
    conditions_passed = row.get("conditions_passed")
    action_taken = row.get("action_taken")
    policy_name = row["policy_name"]
    rule_name = row["rule_name"]

    if rate_limited:
        marker = "⊘"
    elif conditions_passed:
        marker = "✓"
    else:
        marker = "✗"

    lines = [f"  {marker} {policy_name}", f"    Rule: {rule_name}"]

    if rate_limited:
        rl_entry = next(
            (l for l in action_logs
             if l.get("recipe") == policy_name and l.get("action_type") == "rate_limited"),
            None,
        )
        lines.append(f"    Trigger: matched ({event_type})")
        if rl_entry:
            try:
                d = json.loads(rl_entry["action_detail"])
                fires = d.get("fires_in_window", "?")
                max_fires = d.get("max_fires", "?")
                window = d.get("window", "?")
                lines.append(f"    Rate limited: yes ({fires}/{max_fires} fires in {window} window)")
            except (json.JSONDecodeError, TypeError):
                lines.append("    Rate limited: yes")
        else:
            lines.append("    Rate limited: yes")
    else:
        lines.append(f"    Trigger: matched ({event_type})")
        cond_json = row.get("condition_details")
        if cond_json:
            try:
                cond_details = json.loads(cond_json)
                if conditions_passed:
                    parts = []
                    for c in cond_details:
                        if c.get("passed") and c.get("passed") != "not_evaluated":
                            field = c.get("field", "?")
                            op = c.get("op", "?")
                            expected = c.get("expected", "?")
                            actual = c.get("actual")
                            parts.append(f"{field} {op} {expected} → actual: {actual}")
                    cond_str = "; ".join(parts) if parts else "all passed"
                    lines.append(f"    Conditions: passed ({cond_str})")
                else:
                    failed = next(
                        (c for c in cond_details if c.get("passed") is False),
                        None,
                    )
                    if failed:
                        field = failed.get("field", "?")
                        op = failed.get("op", "?")
                        expected = failed.get("expected", "?")
                        actual = failed.get("actual")
                        lines.append(
                            f"    Conditions: failed ({field} {op} {expected} → actual: {actual})"
                        )
                    else:
                        lines.append("    Conditions: failed")
            except (json.JSONDecodeError, TypeError):
                status = "passed" if conditions_passed else "failed"
                lines.append(f"    Conditions: {status}")
        elif conditions_passed is not None:
            status = "passed" if conditions_passed else "failed"
            lines.append(f"    Conditions: {status}")

        if action_taken:
            relevant = [
                l for l in action_logs
                if l.get("recipe") == policy_name or l.get("recipe") == rule_name
            ]
            if relevant:
                for al in relevant:
                    atype = al["action_type"]
                    status = al["status"]
                    err = (al.get("error_message") or "")[:80]
                    if err:
                        lines.append(f"    Actions: {atype} → {status} ({err})")
                    else:
                        lines.append(f"    Actions: {atype} → {status}")
            else:
                lines.append("    Actions: taken")

    return "\n".join(lines)


def cmd_trace(args):
    db = EventsDB(DB_PATH)

    # Mode: --policy [--since] without event_id — show policy trace over time
    if args.policy and args.event_id is None:
        since = args.since or 24
        evals = db.get_policy_evals_since(args.policy, since)
        db.close()
        if not evals:
            print(f"No evaluations found for policy '{args.policy}' in the last {since}h.")
            return
        print(f"Policy: {args.policy} (last {since}h)\n")
        for row in evals:
            event_id = row["event_id"]
            event_type = row.get("event_type", "?")
            event_ts = row.get("event_created_at", "?")
            rate_limited = row.get("rate_limited")
            conditions_passed = row.get("conditions_passed")
            if rate_limited:
                marker = "⊘"
            elif conditions_passed:
                marker = "✓"
            else:
                marker = "✗"
            print(f"  {marker} Event #{event_id}: {event_type} ({event_ts})")
            print(f"    Rule: {row['rule_name']}")
            if rate_limited:
                print("    Rate limited: yes")
            elif conditions_passed is not None:
                status = "passed" if conditions_passed else "failed"
                print(f"    Conditions: {status}")
            print()
        return

    # Mode: <event-id> trace
    if args.event_id is None:
        print("Usage: hex-events trace <event-id> [--policy <name>]")
        print("       hex-events trace --policy <name> [--since <hours>]")
        db.close()
        return

    rows = db.conn.execute("SELECT * FROM events WHERE id = ?", (args.event_id,)).fetchall()
    if not rows:
        print(f"Event {args.event_id} not found.")
        db.close()
        return

    event = dict(rows[0])
    event_type = event["event_type"]
    created_at = event["created_at"]
    print(f"Event #{event['id']}: {event_type} ({created_at})\n")

    policy_evals = db.get_policy_evals(args.event_id, policy_name=args.policy)
    action_logs = db.get_action_logs(args.event_id)
    db.close()

    print("Policy evaluations:\n")

    # Group by policy name
    eval_by_policy: dict[str, list] = {}
    for row in policy_evals:
        pname = row["policy_name"]
        eval_by_policy.setdefault(pname, []).append(row)

    shown: set[str] = set()
    for pname, evals in eval_by_policy.items():
        for row in evals:
            print(_format_trace_row(event_type, row, action_logs))
            print()
        shown.add(pname)

    # Show policies with no log entries (trigger didn't match) — only without a policy filter
    if not args.policy:
        try:
            policies, _ = _load_all_policies()
        except Exception:
            policies = []
        for policy in policies:
            if policy.name not in shown:
                for rule in policy.rules:
                    print(f"  ✗ {policy.name}")
                    print(f"    Rule: {rule.name}")
                    print(
                        f"    Trigger: no match (expected: {rule.trigger_event}, got: {event_type})"
                    )
                    print()

    if not policy_evals and not args.policy:
        try:
            policies, _ = _load_all_policies()
        except Exception:
            policies = []
        if not policies:
            print("  (no policy evaluations logged for this event)")


def _parse_etime(etime_str: str) -> str:
    """Convert ps etime ([[DD-]HH:]MM:SS) to human-readable uptime string."""
    s = etime_str.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    if len(parts) == 3:
        hours = int(parts[0]) + days * 24
        mins = int(parts[1])
    elif len(parts) == 2:
        hours = days * 24
        mins = int(parts[0])
    else:
        return etime_str.strip()
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _last_daemon_activity(log_file: str) -> str:
    """Return human-readable time since last daemon log entry."""
    if not os.path.exists(log_file):
        return "unknown"
    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 50000)
            f.seek(-chunk, 2)
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            # Log lines start with datetime: "2026-03-19 17:00:01,123 ..."
            parts = line.split()
            if len(parts) >= 2:
                try:
                    ts = datetime.strptime(parts[0] + " " + parts[1], "%Y-%m-%d %H:%M:%S,%f")
                    delta = datetime.utcnow() - ts
                    total_secs = int(delta.total_seconds())
                    if total_secs < 60:
                        return f"{total_secs}s ago"
                    mins = total_secs // 60
                    if mins < 60:
                        return f"{mins}m ago"
                    return f"{mins // 60}h {mins % 60}m ago"
                except ValueError:
                    continue
    except Exception:
        pass
    return "unknown"


def cmd_telemetry(args):
    db = EventsDB(DB_PATH)

    events_processed = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE created_at >= datetime('now', '-24 hours')"
    ).fetchone()["cnt"]

    actions_fired = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM action_log al "
        "JOIN events e ON e.id = al.event_id "
        "WHERE e.created_at >= datetime('now', '-24 hours') "
        "AND al.action_type != 'rate_limited' "
        "AND al.status NOT IN ('suppressed', 'error') "
        "AND al.status NOT LIKE 'retry_%'"
    ).fetchone()["cnt"]

    actions_failed = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM action_log al "
        "JOIN events e ON e.id = al.event_id "
        "WHERE e.created_at >= datetime('now', '-24 hours') "
        "AND al.status = 'error'"
    ).fetchone()["cnt"]

    rate_limits_hit = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM action_log al "
        "JOIN events e ON e.id = al.event_id "
        "WHERE e.created_at >= datetime('now', '-24 hours') "
        "AND al.action_type = 'rate_limited'"
    ).fetchone()["cnt"]

    policy_violations = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM events "
        "WHERE created_at >= datetime('now', '-24 hours') "
        "AND event_type LIKE '%violation%'"
    ).fetchone()["cnt"]

    top_policies = db.conn.execute(
        "SELECT pel.policy_name, COUNT(*) as fires "
        "FROM policy_eval_log pel "
        "JOIN events e ON e.id = pel.event_id "
        "WHERE e.created_at >= datetime('now', '-24 hours') "
        "AND pel.action_taken = 1 "
        "GROUP BY pel.policy_name ORDER BY fires DESC LIMIT 5"
    ).fetchall()

    errors = db.conn.execute(
        "SELECT al.recipe, al.action_type, COUNT(*) as cnt "
        "FROM action_log al "
        "JOIN events e ON e.id = al.event_id "
        "WHERE e.created_at >= datetime('now', '-24 hours') "
        "AND al.status = 'error' "
        "GROUP BY al.recipe, al.action_type ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    db.close()

    if getattr(args, "json", False):
        print(json.dumps({
            "events_processed": events_processed,
            "actions_fired": actions_fired,
            "actions_failed": actions_failed,
            "rate_limits_hit": rate_limits_hit,
            "policy_violations": policy_violations,
        }))
        return

    # Daemon info
    result = subprocess.run(["pgrep", "-f", "hex_eventd"], capture_output=True, text=True)
    daemon_running = result.returncode == 0
    pid = result.stdout.strip().split("\n")[0] if daemon_running else None

    uptime_str = "not running"
    if daemon_running and pid:
        try:
            ps = subprocess.run(
                ["ps", "-o", "etime=", "-p", pid], capture_output=True, text=True
            )
            if ps.returncode == 0 and ps.stdout.strip():
                uptime_str = _parse_etime(ps.stdout)
        except Exception:
            uptime_str = "unknown"

    log_file = os.path.join(BASE_DIR, "daemon.log")
    last_heartbeat_str = _last_daemon_activity(log_file)

    log_size_str = "N/A"
    rotations = 0
    if os.path.exists(log_file):
        try:
            size_bytes = os.path.getsize(log_file)
            for i in range(1, 10):
                if os.path.exists(log_file + f".{i}"):
                    rotations += 1
                else:
                    break
            if size_bytes >= 1024 * 1024:
                log_size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
            else:
                log_size_str = f"{size_bytes / 1024:.1f} KB"
        except Exception:
            pass

    print("hex-events Telemetry (last 24h)")
    print("────────────────────────────")
    print(f"Events processed:     {events_processed}")
    print(f"Actions fired:        {actions_fired}")
    print(f"Actions failed:       {actions_failed}")
    print(f"Rate limits hit:      {rate_limits_hit}")
    print(f"Policy violations:    {policy_violations}")

    if top_policies:
        print()
        print("Top policies:")
        for row in top_policies:
            print(f"  {row['policy_name']:<30s}  {row['fires']} fires")

    if errors:
        print()
        print("Errors:")
        for row in errors:
            print(f"  {row['recipe']}: {row['cnt']} failures ({row['action_type']} action)")

    print()
    print("Daemon:")
    print(f"  Uptime: {uptime_str}")
    print(f"  Last heartbeat: {last_heartbeat_str}")
    if os.path.exists(log_file):
        print(f"  Log size: {log_size_str} ({rotations} rotations)")


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
    from policy_validator import validate_policy_file

    # Determine which files to validate
    if hasattr(args, "file") and args.file:
        files = [args.file]
    else:
        policies_dir = POLICIES_DIR if os.path.isdir(POLICIES_DIR) else RECIPES_DIR
        if not os.path.isdir(policies_dir):
            print("No policies directory found.")
            sys.exit(0)
        files = sorted(
            os.path.join(policies_dir, f)
            for f in os.listdir(policies_dir)
            if f.endswith(".yaml") or f.endswith(".yml")
        )

    valid_count = 0
    invalid_count = 0

    for filepath in files:
        errors = validate_policy_file(filepath)
        try:
            import yaml
            with open(filepath) as f:
                policy = yaml.safe_load(f)
            rule_count = len(policy.get("rules", [])) if isinstance(policy, dict) else 0
        except Exception:
            rule_count = 0

        display_path = os.path.relpath(filepath) if os.path.isabs(filepath) else filepath

        if errors:
            invalid_count += 1
            print(f"{display_path}: ERROR")
            for err in errors:
                # Strip leading filename from error message for cleaner output
                msg = err
                if msg.startswith(filepath + ":"):
                    msg = msg[len(filepath) + 1:].strip()
                elif msg.startswith(filepath + " "):
                    msg = msg[len(filepath):].strip()
                print(f"  - {msg}")
        else:
            valid_count += 1
            print(f"{display_path}: OK ({rule_count} rules)")

    print()
    print(f"Summary: {valid_count} valid, {invalid_count} invalid")

    if invalid_count > 0:
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

    val_p = sub.add_parser("validate", help="Validate policy schema for all or a specific file")
    val_p.add_argument("file", nargs="?", help="Specific policy file to validate (default: all)")

    graph_p = sub.add_parser("graph", help="Show event dependency graph")

    trace_p = sub.add_parser("trace", help="Show policy evaluation trace for an event")
    trace_p.add_argument("event_id", type=int, nargs="?", help="Event ID to trace")
    trace_p.add_argument("--policy", help="Filter to a specific policy name")
    trace_p.add_argument("--since", type=int, default=None,
                         help="Hours to look back (used with --policy to show history)")
    graph_p.add_argument("--observed", action="store_true",
                         help="Show observed graph from DB (last 7 days)")

    telem_p = sub.add_parser("telemetry", help="Show unified telemetry health overview")
    telem_p.add_argument("--json", action="store_true", help="Output as JSON")

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
    elif args.command == "trace":
        cmd_trace(args)
    elif args.command == "telemetry":
        cmd_telemetry(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
