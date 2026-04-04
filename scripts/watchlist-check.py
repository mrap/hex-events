#!/usr/bin/env python3
"""watchlist-check.py — Check watchlist items for developments.

Reads me/watchlist.md, extracts GitHub repos and X accounts,
checks for recent activity, and reports notable changes.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


AGENT_DIR = os.environ.get("AGENT_DIR", os.path.expanduser("~/hex"))
WATCHLIST = Path(AGENT_DIR) / "me" / "watchlist.md"
STATE_FILE = Path(os.path.expanduser("~/.hex-events/watchlist-state.json"))

# Attribution source for events emitted by this script or its policy
HEX_SOURCE = "hex:watchlist"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def parse_watchlist():
    """Extract watchlist entries with GitHub URLs and X handles."""
    if not WATCHLIST.exists():
        return []

    content = WATCHLIST.read_text()
    entries = []

    for line in content.split("\n"):
        if not line.startswith("|") or line.startswith("| #") or line.startswith("|---"):
            continue

        # Extract GitHub URLs
        gh_matches = re.findall(r"github\.com/([\w-]+/[\w-]+)", line)
        # Extract X/Twitter handles
        x_matches = re.findall(r"x\.com/([\w]+)", line)

        if gh_matches or x_matches:
            # Extract the "What" column (second pipe-delimited field)
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 3:
                entries.append({
                    "id": cols[1].strip(),
                    "what": cols[2].strip(),
                    "github_repos": gh_matches,
                    "x_accounts": x_matches,
                })

    return entries


def check_github_repo(repo):
    """Check repo for star count and recent activity."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--jq",
             '{stars: .stargazers_count, pushed: .pushed_at, forks: .forks_count}'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def check_developments(entries, state):
    """Compare current state to previous, flag notable changes."""
    changes = []
    new_state = {}

    for entry in entries:
        for repo in entry.get("github_repos", []):
            info = check_github_repo(repo)
            if not info:
                continue

            key = f"gh:{repo}"
            new_state[key] = info
            prev = state.get(key)

            if prev:
                star_delta = info["stars"] - prev.get("stars", 0)
                if star_delta > 500:
                    changes.append(
                        f"[{repo}] +{star_delta} stars "
                        f"({prev.get('stars', '?')} → {info['stars']})"
                    )
                if info.get("pushed") != prev.get("pushed"):
                    changes.append(
                        f"[{repo}] New push: {info['pushed']}"
                    )
            else:
                # First check — record baseline
                changes.append(
                    f"[{repo}] Baseline: {info['stars']} stars, "
                    f"last push {info.get('pushed', 'unknown')}"
                )

    return changes, new_state


def update_last_checked():
    """Update the 'Last checked' column in the table for all entries."""
    if not WATCHLIST.exists():
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = WATCHLIST.read_text()
    # Replace dates in the Last checked column (YYYY-MM-DD pattern in table rows)
    updated = re.sub(
        r"(\| \d+ \|[^|]+\|[^|]+\|) \d{4}-\d{2}-\d{2} (\|)",
        rf"\1 {today} \2",
        content,
    )
    WATCHLIST.write_text(updated)


def append_to_log(changes, new_state):
    """Append check results to the Log section of watchlist.md."""
    if not WATCHLIST.exists():
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = WATCHLIST.read_text()

    # Build log entry
    lines = [f"\n### {today} — Automated check"]
    for c in changes:
        lines.append(f"- {c}")

    # Also log current stats for items with no changes
    for key, info in new_state.items():
        repo = key.replace("gh:", "")
        already_mentioned = any(repo in c for c in changes)
        if not already_mentioned:
            lines.append(
                f"- **{repo}**: {info['stars']} stars, "
                f"last push {info.get('pushed', 'unknown')}. No notable change."
            )

    log_entry = "\n".join(lines) + "\n"

    # Insert after the last line of the file
    with open(WATCHLIST, "a") as f:
        f.write(log_entry)


def main():
    entries = parse_watchlist()
    if not entries:
        print("No watchlist entries found.")
        return

    state = load_state()
    changes, new_state = check_developments(entries, state)

    # Merge new state
    state.update(new_state)
    state["_last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # Always log to watchlist.md and update table dates
    update_last_checked()
    append_to_log(changes, new_state)

    if changes:
        print("Watchlist developments:")
        for c in changes:
            print(f"  - {c}")
        # Exit 1 to trigger notification via hex-events
        sys.exit(1)
    else:
        print("No notable changes.")
        sys.exit(0)


if __name__ == "__main__":
    main()
