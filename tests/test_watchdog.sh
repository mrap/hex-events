#!/bin/bash
# Smoke test for hex-watchdog.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WATCHDOG="$SCRIPT_DIR/scripts/hex-watchdog.sh"

echo "Testing watchdog script exists and is executable..."
[[ -x "$WATCHDOG" ]] || { echo "FAIL: watchdog not executable"; exit 1; }

echo "Testing watchdog runs without error..."
bash "$WATCHDOG" 2>/dev/null
[[ $? -eq 0 ]] || { echo "FAIL: watchdog exited with error"; exit 1; }

echo "Testing watchdog log was created..."
[[ -f "${HOME}/.hex-events/watchdog.log" ]] || { echo "FAIL: no log file"; exit 1; }

echo "Testing log has recent entries..."
last_line=$(tail -1 "${HOME}/.hex-events/watchdog.log")
[[ -n "$last_line" ]] || { echo "FAIL: log is empty"; exit 1; }

echo "ALL TESTS PASSED"
