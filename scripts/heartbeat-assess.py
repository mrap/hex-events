#!/usr/bin/env python3
"""
heartbeat-assess.py: Read heartbeat JSON from stdin, decide if notification needed.

Exit code 0 = no notification needed
Exit code 1 = notification needed
"""

import json
import sys
import os
from datetime import datetime, timezone

LAST_HEARTBEAT_PATH = os.path.expanduser("~/.hex-events/last-heartbeat.json")


def load_last_heartbeat():
    try:
        with open(LAST_HEARTBEAT_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save_current_heartbeat(data):
    try:
        os.makedirs(os.path.dirname(LAST_HEARTBEAT_PATH), exist_ok=True)
        with open(LAST_HEARTBEAT_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def is_work_hours(dt):
    """Return True if dt is between 9am and 7pm local time."""
    return 9 <= dt.hour < 19


def assess(current, last):
    reasons = []

    # 1. BOI failures increased
    try:
        current_failures = current.get("boi", {}).get("failed") or 0
        last_failures = (last or {}).get("boi", {}).get("failed") or 0
        if current_failures > last_failures:
            reasons.append("boi failures increased")
    except Exception:
        pass

    # 2. Landings completion stalled (same done count for 2+ hours during work hours)
    try:
        now = datetime.fromisoformat(current["timestamp"].replace("Z", "+00:00"))
        # Convert to local time for work hours check
        now_local = datetime.now()
        if is_work_hours(now_local) and last is not None:
            current_done = (current.get("landings") or {}).get("done")
            last_done = (last.get("landings") or {}).get("done")
            last_ts = last.get("timestamp")
            if (
                current_done is not None
                and last_done is not None
                and current_done == last_done
                and last_ts is not None
            ):
                last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                try:
                    current_dt = datetime.fromisoformat(
                        current["timestamp"].replace("Z", "+00:00")
                    )
                    elapsed_hours = (
                        current_dt - last_dt
                    ).total_seconds() / 3600
                    if elapsed_hours >= 2:
                        reasons.append("stalled landings")
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Unread messages > 10
    try:
        unread = current.get("unread_msgs")
        if unread is not None and unread > 10:
            reasons.append("high unread messages")
    except Exception:
        pass

    # 4. hex-events daemon not healthy
    try:
        health = current.get("events_health")
        if health is not None and health != "ok":
            reasons.append("hex-events daemon unhealthy")
    except Exception:
        pass

    return reasons


def main():
    try:
        raw = sys.stdin.read()
        current = json.loads(raw)
    except Exception as e:
        print(json.dumps({"notify": False, "reasons": [], "error": str(e)}))
        sys.exit(0)

    last = load_last_heartbeat()
    reasons = assess(current, last)
    save_current_heartbeat(current)

    notify = len(reasons) > 0
    print(json.dumps({"notify": notify, "reasons": reasons}))
    sys.exit(1 if notify else 0)


if __name__ == "__main__":
    main()
