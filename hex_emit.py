#!/usr/bin/env python3
"""hex-emit — lightweight event emitter for trigger adapters.

Usage: hex-emit <event_type> [payload_json] [source]
       hex-emit --db /path/to/db <event_type> [payload_json] [source]

Designed to be fast (no recipe loading, no daemon). Just INSERT and exit.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import EventsDB

DEFAULT_DB = os.path.expanduser("~/.hex-events/events.db")

def main():
    parser = argparse.ArgumentParser(description="Emit a hex-event")
    parser.add_argument("event_type", help="Event type (e.g., boi.spec.completed)")
    parser.add_argument("payload", nargs="?", default="{}", help="JSON payload")
    parser.add_argument("source", nargs="?", default="hex-emit", help="Event source")
    parser.add_argument("--db", default=DEFAULT_DB, help="Database path")
    args = parser.parse_args()

    db = EventsDB(args.db)
    eid = db.insert_event(args.event_type, args.payload, args.source)
    db.close()
    print(f"Event {eid}: {args.event_type}")

if __name__ == "__main__":
    main()
