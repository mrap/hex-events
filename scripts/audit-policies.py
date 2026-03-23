#!/usr/bin/env python3
"""
hex-events Policy Audit Script
Reads all policies, queries the events DB, and generates a health report.
Usage:
  python3 ~/.hex-events/scripts/audit-policies.py          # markdown report
  python3 ~/.hex-events/scripts/audit-policies.py --json   # JSON output
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

HEX_HOME = Path(os.environ.get("HEX_HOME", Path.home() / ".hex-events"))
POLICIES_DIR = HEX_HOME / "policies"
DB_PATH = HEX_HOME / "events.db"
WINDOW_DAYS = 7


def load_policies():
    """Parse all policy YAML files. Returns list of dicts with policy metadata."""
    policies = []
    for path in sorted(POLICIES_DIR.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not data or "name" not in data:
                continue
            rules = data.get("rules", [])
            trigger_events = []
            rule_names = []
            for rule in rules:
                rule_names.append(rule.get("name", ""))
                trigger = rule.get("trigger", {})
                ev = trigger.get("event")
                if ev and ev not in trigger_events:
                    trigger_events.append(ev)
            rate_limit = data.get("rate_limit", {})
            policies.append({
                "name": data["name"],
                "description": data.get("description", ""),
                "trigger_events": trigger_events,
                "rule_names": rule_names,
                "rate_limit": rate_limit,
                "path": str(path),
            })
        except Exception as e:
            print(f"WARN: Failed to parse {path}: {e}", file=sys.stderr)
    return policies


def query_db(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def audit_policy(policy, db_path):
    """Compute health metrics for a single policy."""
    name = policy["name"]
    trigger_events = policy["trigger_events"]
    rule_names = policy["rule_names"]

    # Count trigger events in last 7 days
    if trigger_events:
        placeholders = ",".join("?" * len(trigger_events))
        rows = query_db(
            db_path,
            f"SELECT COUNT(*) as cnt FROM events "
            f"WHERE event_type IN ({placeholders}) "
            f"AND created_at > datetime('now', '-{WINDOW_DAYS} days')",
            trigger_events,
        )
        trigger_count = rows[0]["cnt"] if rows else 0
    else:
        trigger_count = 0

    # Count fires (action_taken=1) from policy_eval_log in last 7 days
    rows = query_db(
        db_path,
        f"SELECT COUNT(*) as cnt FROM policy_eval_log "
        f"WHERE policy_name = ? AND action_taken = 1 "
        f"AND evaluated_at > datetime('now', '-{WINDOW_DAYS} days')",
        (name,),
    )
    fires = rows[0]["cnt"] if rows else 0

    # Count evals where conditions blocked (matched but conditions_passed=0)
    rows = query_db(
        db_path,
        f"SELECT COUNT(*) as cnt FROM policy_eval_log "
        f"WHERE policy_name = ? AND matched = 1 AND conditions_passed = 0 "
        f"AND evaluated_at > datetime('now', '-{WINDOW_DAYS} days')",
        (name,),
    )
    conditions_blocked = rows[0]["cnt"] if rows else 0

    # Count evals where rate limited
    rows = query_db(
        db_path,
        f"SELECT COUNT(*) as cnt FROM policy_eval_log "
        f"WHERE policy_name = ? AND rate_limited = 1 "
        f"AND evaluated_at > datetime('now', '-{WINDOW_DAYS} days')",
        (name,),
    )
    rate_limited_count = rows[0]["cnt"] if rows else 0

    # Count action success/failure from action_log (by rule names)
    action_success = 0
    action_error = 0
    if rule_names:
        placeholders = ",".join("?" * len(rule_names))
        rows = query_db(
            db_path,
            f"SELECT status, COUNT(*) as cnt FROM action_log "
            f"WHERE recipe IN ({placeholders}) "
            f"AND executed_at > datetime('now', '-{WINDOW_DAYS} days') "
            f"GROUP BY status",
            rule_names,
        )
        for r in rows:
            st = r["status"]
            if st == "success":
                action_success += r["cnt"]
            elif st == "error":
                action_error += r["cnt"]
            # retry_1, retry_2, retry_3 are intermediate — ignore for final tally

    total_actions = action_success + action_error

    # Compute rates
    fire_rate = fires / trigger_count if trigger_count > 0 else None
    success_rate = action_success / total_actions if total_actions > 0 else None

    # Health classification
    if trigger_count == 0:
        # Check if it ever had trigger events at all
        if trigger_events:
            placeholders = ",".join("?" * len(trigger_events))
            rows = query_db(
                db_path,
                f"SELECT COUNT(*) as cnt FROM events WHERE event_type IN ({placeholders})",
                trigger_events,
            )
            ever_count = rows[0]["cnt"] if rows else 0
        else:
            ever_count = 0

        status = "dead" if ever_count > 0 else "untested"
    elif fires == 0:
        status = "silent"
    elif success_rate is not None and success_rate < 0.90:
        status = "degraded"
    else:
        status = "healthy"

    # Diagnostic: most recent trigger event that didn't result in action
    last_missed_event = None
    if status in ("silent", "degraded") and trigger_events:
        placeholders = ",".join("?" * len(trigger_events))
        rows = query_db(
            db_path,
            f"SELECT event_type, payload, created_at FROM events "
            f"WHERE event_type IN ({placeholders}) "
            f"AND created_at > datetime('now', '-{WINDOW_DAYS} days') "
            f"ORDER BY created_at DESC LIMIT 1",
            trigger_events,
        )
        if rows:
            last_missed_event = rows[0]

    # Most recent policy_eval_log entry for condition details
    recent_eval = None
    if status in ("silent", "degraded"):
        rows = query_db(
            db_path,
            "SELECT rule_name, conditions_passed, condition_details, rate_limited, action_taken, evaluated_at "
            "FROM policy_eval_log WHERE policy_name = ? "
            "ORDER BY evaluated_at DESC LIMIT 1",
            (name,),
        )
        if rows:
            recent_eval = rows[0]

    return {
        "name": name,
        "status": status,
        "trigger_events": trigger_events,
        "trigger_count": trigger_count,
        "fires": fires,
        "conditions_blocked": conditions_blocked,
        "rate_limited_count": rate_limited_count,
        "action_success": action_success,
        "action_error": action_error,
        "fire_rate": fire_rate,
        "success_rate": success_rate,
        "last_missed_event": last_missed_event,
        "recent_eval": recent_eval,
        "rate_limit": policy["rate_limit"],
    }


def fmt_pct(val):
    if val is None:
        return "—"
    return f"{val:.0%}"


def fmt_issue(result):
    """Generate a short issue description."""
    if result["status"] == "healthy":
        return "—"
    if result["status"] == "dead":
        return f"{result['trigger_events'][0] if result['trigger_events'] else '?'} not emitting"
    if result["status"] == "untested":
        return "no events ever; policy may be new or misconfigured"
    if result["status"] == "silent":
        if result["rate_limited_count"] > 0:
            return f"rate limited {result['rate_limited_count']}x"
        if result["conditions_blocked"] > 0:
            return "conditions always block"
        if not result["fires"]:
            return "no policy_eval_log entries; may not be evaluated"
    if result["status"] == "degraded":
        err_pct = 1.0 - (result["success_rate"] or 0)
        return f"action fails {err_pct:.0%} of the time"
    return "unknown"


def generate_markdown(policies_data, results):
    counts = {"healthy": 0, "degraded": 0, "silent": 0, "dead": 0, "untested": 0}
    for r in results:
        counts[r["status"]] += 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# hex-events Policy Audit — {today}",
        "",
        "## Summary",
        f"- Policies: {len(results)}",
        f"- Healthy: {counts['healthy']}",
        f"- Degraded: {counts['degraded']}",
        f"- Silent: {counts['silent']}",
        f"- Dead: {counts['dead']}",
        f"- Untested: {counts['untested']}",
        "",
        "## Policy Report Card",
        "",
        "| Policy | Status | Trigger Events (7d) | Fires | Success Rate | Fire Rate | Issue |",
        "|--------|--------|--------------------:|------:|-------------:|----------:|-------|",
    ]

    for r in results:
        status_icon = {
            "healthy": "Healthy",
            "degraded": "Degraded",
            "silent": "Silent",
            "dead": "Dead",
            "untested": "Untested",
        }[r["status"]]
        trigger_str = ", ".join(r["trigger_events"]) if r["trigger_events"] else "—"
        lines.append(
            f"| {r['name']} | {status_icon} | {r['trigger_count']} | {r['fires']} | "
            f"{fmt_pct(r['success_rate'])} | {fmt_pct(r['fire_rate'])} | {fmt_issue(r)} |"
        )

    # Diagnostics section
    problem_results = [r for r in results if r["status"] in ("silent", "degraded", "dead", "untested")]
    if problem_results:
        lines += ["", "## Diagnostics", ""]
        for r in problem_results:
            lines.append(f"### {r['name']} ({r['status'].capitalize()})")
            trigger_str = ", ".join(r["trigger_events"]) if r["trigger_events"] else "none"
            lines.append(f"- Trigger event(s): {trigger_str}")
            lines.append(f"- Trigger events (7d): {r['trigger_count']}")
            lines.append(f"- Policy fires (7d): {r['fires']}")

            if r["status"] == "dead":
                lines.append(f"- Diagnosis: trigger source `{trigger_str}` emitting 0 events in last {WINDOW_DAYS} days")
                lines.append(f"- Recommendation: check adapter config for `{trigger_str}`")

            elif r["status"] == "untested":
                lines.append(f"- Diagnosis: no events of type `{trigger_str}` have ever been recorded")
                lines.append("- Recommendation: verify policy trigger event type is correct; check if source is configured")

            elif r["status"] == "silent":
                if r["conditions_blocked"] > 0:
                    lines.append(f"- Conditions blocked firing: {r['conditions_blocked']} times")
                if r["rate_limited_count"] > 0:
                    lines.append(f"- Rate limited: {r['rate_limited_count']} times")
                    rl = r["rate_limit"]
                    if rl:
                        lines.append(f"- Rate limit config: max_fires={rl.get('max_fires')}, window={rl.get('window')}")
                if r["recent_eval"]:
                    ev = r["recent_eval"]
                    lines.append(f"- Most recent eval: rule={ev['rule_name']}, "
                                 f"conditions_passed={ev['conditions_passed']}, "
                                 f"rate_limited={ev['rate_limited']}, "
                                 f"action_taken={ev['action_taken']}")
                    if ev.get("condition_details"):
                        lines.append(f"- Condition details: {ev['condition_details']}")
                if r["last_missed_event"]:
                    lme = r["last_missed_event"]
                    lines.append(f"- Last unactioned trigger: {lme['event_type']} at {lme['created_at']}")
                lines.append(f"- Recommendation: inspect condition logic or rate limit settings")

            elif r["status"] == "degraded":
                lines.append(f"- Action successes (7d): {r['action_success']}")
                lines.append(f"- Action errors (7d): {r['action_error']}")
                err_pct = 1.0 - (r["success_rate"] or 0)
                lines.append(f"- Failure rate: {err_pct:.0%}")
                if r["recent_eval"]:
                    ev = r["recent_eval"]
                    lines.append(f"- Most recent eval: rule={ev['rule_name']}, action_taken={ev['action_taken']}")
                lines.append("- Recommendation: check action_log for error_message details")

            lines.append("")

    return "\n".join(lines)


def generate_json(results):
    counts = {"healthy": 0, "degraded": 0, "silent": 0, "dead": 0, "untested": 0}
    for r in results:
        counts[r["status"]] += 1

    output = {
        "total": len(results),
        "healthy": counts["healthy"],
        "degraded": counts["degraded"],
        "silent": counts["silent"],
        "dead": counts["dead"],
        "untested": counts["untested"],
        "policies": [
            {
                "name": r["name"],
                "status": r["status"],
                "trigger_events": r["trigger_events"],
                "trigger_events_7d": r["trigger_count"],
                "fires": r["fires"],
                "action_success": r["action_success"],
                "action_error": r["action_error"],
                "success_rate": r["success_rate"],
                "fire_rate": r["fire_rate"],
            }
            for r in results
        ],
    }
    return json.dumps(output, indent=2)


def main():
    parser = argparse.ArgumentParser(description="hex-events policy audit")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    policies = load_policies()
    if not policies:
        print("ERROR: No policies found in " + str(POLICIES_DIR), file=sys.stderr)
        sys.exit(1)

    results = [audit_policy(p, str(DB_PATH)) for p in policies]

    if args.json:
        print(generate_json(results))
    else:
        print(generate_markdown(policies, results))


if __name__ == "__main__":
    main()
