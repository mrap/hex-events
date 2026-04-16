#!/usr/bin/env python3
"""hex-lineage — trace attribution and lineage through hex-events history.

Usage:
  python3 hex-lineage.py R-047              # Trace issue lifecycle
  python3 hex-lineage.py q-224              # Trace spec origin and completion
  python3 hex-lineage.py --auto-only --since 7d   # Hex-initiated actions
  python3 hex-lineage.py --source user --since 7d  # User-initiated actions
  python3 hex-lineage.py --summary --since 30d     # Count by source
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DEFAULT_DB = os.path.expanduser("~/.hex-events/events.db")


def parse_since(since_str: str) -> str:
    """Convert --since arg (e.g. 7d, 2h, 30m, 1h) to SQLite datetime offset."""
    suffixes = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    s = since_str.strip()
    if s[-1] in suffixes:
        num = s[:-1]
        try:
            int(num)
        except ValueError:
            sys.exit(f"Invalid --since value: {since_str!r}")
        return f"-{num} {suffixes[s[-1]]}"
    # bare number: treat as hours
    try:
        int(s)
    except ValueError:
        sys.exit(f"Invalid --since value: {since_str!r}")
    return f"-{s} hours"


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        sys.exit(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def safe_parse_payload(raw: str) -> dict:
    """Parse JSON payload, return empty dict on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def format_event(row: sqlite3.Row, indent: str = "  ") -> str:
    payload = safe_parse_payload(row["payload"])
    ts = row["created_at"] or "?"
    source = row["source"] or "unknown"
    etype = row["event_type"]
    eid = row["id"]

    # Build a concise context line from payload fields
    ctx_parts = []
    for key in ("spec_id", "reason", "parent_issue_id", "score", "session_id",
                 "issues_detected", "fixes_applied", "delta_path"):
        val = payload.get(key)
        if val is not None:
            ctx_parts.append(f"{key}={val}")

    ctx = f" [{', '.join(ctx_parts)}]" if ctx_parts else ""
    return f"{indent}[{ts}] #{eid} {etype} via {source}{ctx}"


def cmd_trace_issue(conn: sqlite3.Connection, issue_id: str, since_offset: str | None):
    """Trace an issue (R-NNN) through the event log."""
    print(f"Lineage trace for issue: {issue_id}")
    print("=" * 60)

    query = "SELECT * FROM events WHERE payload LIKE ? ORDER BY id"
    params: list = [f"%{issue_id}%"]

    if since_offset:
        query = "SELECT * FROM events WHERE payload LIKE ? AND created_at >= datetime('now', ?) ORDER BY id"
        params.append(since_offset)

    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error as e:
        print(f"  Error querying events: {e}", file=sys.stderr)
        return

    if not rows:
        print(f"  No events found referencing {issue_id}")
        return

    for row in rows:
        print(format_event(row))
    print(f"\n{len(rows)} event(s) found.")


def cmd_trace_spec(conn: sqlite3.Connection, spec_id: str, since_offset: str | None):
    """Trace a spec (q-NNN) from dispatch to completion."""
    print(f"Lineage trace for spec: {spec_id}")
    print("=" * 60)

    base_query = (
        "SELECT * FROM events WHERE (payload LIKE ? OR event_type LIKE ?) "
        "{since_clause} ORDER BY id"
    )
    params: list = [f"%{spec_id}%", f"%{spec_id}%"]

    if since_offset:
        since_clause = "AND created_at >= datetime('now', ?)"
        params.append(since_offset)
    else:
        since_clause = ""

    query = base_query.format(since_clause=since_clause)
    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error as e:
        print(f"  Error querying events: {e}", file=sys.stderr)
        return

    # Fallback: search by event type for common boi events
    if not rows:
        boi_query = (
            "SELECT * FROM events WHERE event_type IN "
            "('boi.spec.dispatched','boi.spec.completed','boi.spec.failed') "
            "{since_clause} ORDER BY id"
        )
        fallback_params: list = []
        if since_offset:
            boi_query = boi_query.format(since_clause="AND created_at >= datetime('now', ?)")
            fallback_params.append(since_offset)
        else:
            boi_query = boi_query.format(since_clause="")
        try:
            boi_rows = conn.execute(boi_query, fallback_params).fetchall()
        except sqlite3.Error as e:
            print(f"  Error querying events: {e}", file=sys.stderr)
            return

        # Filter those where payload contains spec_id
        rows = [r for r in boi_rows if spec_id in (r["payload"] or "")]

    if not rows:
        print(f"  No events found for spec {spec_id}")
        return

    for row in rows:
        print(format_event(row))
    print(f"\n{len(rows)} event(s) found.")


def cmd_filter(conn: sqlite3.Connection, args):
    """Show events filtered by source and/or --auto-only, with --since."""
    conditions = []
    params: list = []

    if args.since:
        offset = parse_since(args.since)
        conditions.append("created_at >= datetime('now', ?)")
        params.append(offset)

    if args.auto_only:
        conditions.append("source LIKE 'hex:%'")
        label = "hex-initiated"
    elif args.source:
        conditions.append("source = ?")
        params.append(args.source)
        label = f"source={args.source}"
    else:
        label = "all"

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM events {where} ORDER BY id DESC LIMIT 200"
    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error as e:
        print(f"  Error querying events: {e}", file=sys.stderr)
        return

    since_label = f" (last {args.since})" if args.since else ""
    print(f"Events — {label}{since_label}")
    print("=" * 60)

    if not rows:
        print("  No events found matching criteria.")
        return

    for row in reversed(rows):
        print(format_event(row))
    print(f"\n{len(rows)} event(s) found.")


def cmd_summary(conn: sqlite3.Connection, args):
    """Show counts of events grouped by source prefix."""
    conditions = []
    params: list = []

    if args.since:
        offset = parse_since(args.since)
        conditions.append("created_at >= datetime('now', ?)")
        params.append(offset)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT source, COUNT(*) as cnt FROM events {where} GROUP BY source ORDER BY cnt DESC"
    try:
        rows = conn.execute(query, params).fetchall()
    except sqlite3.Error as e:
        print(f"  Error querying events: {e}", file=sys.stderr)
        return

    since_label = f" (last {args.since})" if args.since else " (all time)"
    print(f"Attribution Summary{since_label}")
    print("=" * 60)

    if not rows:
        print("  No events found.")
        return

    # Group by prefix for summary
    prefix_counts: dict[str, int] = {}
    raw_counts: dict[str, int] = {}
    for row in rows:
        source = row["source"] or "unknown"
        cnt = row["cnt"]
        raw_counts[source] = cnt

        if source.startswith("hex:"):
            prefix = "hex (autonomous)"
        elif source == "user":
            prefix = "user (human)"
        elif source == "unknown":
            prefix = "unknown (legacy)"
        else:
            prefix = f"other ({source})"
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + cnt

    # Print grouped summary
    total = sum(prefix_counts.values())
    for label, cnt in sorted(prefix_counts.items(), key=lambda x: -x[1]):
        pct = (cnt / total * 100) if total > 0 else 0
        print(f"  {label:30s}  {cnt:5d}  ({pct:.1f}%)")

    print(f"\n  {'TOTAL':30s}  {total:5d}")
    print()

    # Detailed breakdown
    print("Detailed source breakdown:")
    for source, cnt in sorted(raw_counts.items(), key=lambda x: -x[1]):
        print(f"  {source:40s}  {cnt}")


def main():
    parser = argparse.ArgumentParser(
        description="Trace lineage and attribution in hex-events"
    )
    parser.add_argument(
        "target", nargs="?",
        help="Issue ID (R-NNN) or spec ID (q-NNN) to trace"
    )
    parser.add_argument(
        "--auto-only", action="store_true",
        help="Show only hex-initiated events (source starts with hex:)"
    )
    parser.add_argument(
        "--source",
        help="Filter by exact source value (e.g. user, hex:auto-dispatch)"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Show count summary grouped by source"
    )
    parser.add_argument(
        "--since",
        help="Time window (e.g. 7d, 2h, 30m). Default: all time"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="Path to events.db"
    )
    args = parser.parse_args()

    conn = connect(args.db)

    try:
        if args.summary:
            cmd_summary(conn, args)
        elif args.target:
            since_offset = parse_since(args.since) if args.since else None
            # Determine target type
            if args.target.startswith("R-") or args.target.startswith("r-"):
                cmd_trace_issue(conn, args.target.upper(), since_offset)
            elif args.target.startswith("q-") or args.target.startswith("Q-"):
                cmd_trace_spec(conn, args.target, since_offset)
            else:
                # Generic search
                cmd_trace_issue(conn, args.target, since_offset)
        elif args.auto_only or args.source:
            cmd_filter(conn, args)
        else:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
