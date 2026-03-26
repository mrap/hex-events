#!/usr/bin/env python3
"""hex-emit — lightweight event emitter for trigger adapters.

Usage: hex-emit <event_type> [payload_json] [source]
       hex-emit --db /path/to/db <event_type> [payload_json] [source]

Designed to be fast (no recipe loading, no daemon). Just INSERT and exit.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import EventsDB

DEFAULT_DB = os.path.expanduser("~/.hex-events/events.db")

# Known valid source patterns. Validation is advisory only (warning, not error).
# Pattern: exact string or prefix ending with ":"
VALID_SOURCE_PREFIXES = ("hex:", "mike", "unknown")


def _validate_source(source: str) -> None:
    """Warn if source doesn't match known patterns. Does not block emission."""
    for prefix in VALID_SOURCE_PREFIXES:
        if source == prefix or source.startswith("hex:"):
            return
    print(
        f"[hex-emit] WARNING: unrecognized source '{source}'. "
        f"Expected: mike, unknown, or hex:<name>. Event will still be emitted.",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser(description="Emit a hex-event")
    parser.add_argument("event_type", help="Event type (e.g., boi.spec.completed)")
    parser.add_argument("payload", nargs="?", default="{}", help="JSON payload")
    parser.add_argument("source", nargs="?", default="unknown", help="Event source")
    parser.add_argument("--db", default=DEFAULT_DB, help="Database path")
    args = parser.parse_args()

    _validate_source(args.source)

    try:
        json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"[hex-emit] WARNING: payload is not valid JSON: {e}. Storing raw string.", file=sys.stderr)

    db = EventsDB(args.db)
    eid = db.insert_event(args.event_type, args.payload, args.source)
    db.close()
    print(f"Event {eid}: {args.event_type}")

if __name__ == "__main__":
    main()
