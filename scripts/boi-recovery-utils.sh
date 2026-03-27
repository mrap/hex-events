#!/usr/bin/env bash
# boi-recovery-utils.sh — Helper functions for BOI auto-recovery metadata
# Tracks recovery attempts per spec (max 2) in ~/.boi/recovery/{queue_id}.json
set -uo pipefail

RECOVERY_DIR="$HOME/.boi/recovery"
MAX_RECOVERY_ATTEMPTS=2

# Ensure recovery directory exists
mkdir -p "$RECOVERY_DIR"

# get_recovery_attempts(queue_id)
# Returns the number of recovery attempts for a given spec (0 if no file)
get_recovery_attempts() {
    local queue_id="$1"
    local recovery_file="$RECOVERY_DIR/${queue_id}.json"

    if [[ ! -f "$recovery_file" ]]; then
        echo 0
        return 0
    fi

    local count
    count=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    print(len(data.get('attempts', [])))
except Exception:
    print(0)
" "$recovery_file" 2>/dev/null) || true
    count="${count:-0}"
    echo "$count"
}

# record_recovery_attempt(queue_id, diagnosis, action, details)
# Creates or updates the recovery JSON file with a new attempt entry
record_recovery_attempt() {
    local queue_id="$1"
    local diagnosis="$2"
    local action="$3"
    local details="$4"
    local recovery_file="$RECOVERY_DIR/${queue_id}.json"
    local tmp_file="${recovery_file}.tmp"

    python3 -c "
import json, os, sys
from datetime import datetime, timezone

queue_id = sys.argv[1]
diagnosis = sys.argv[2]
action = sys.argv[3]
details = sys.argv[4]
recovery_file = sys.argv[5]
tmp_file = sys.argv[6]

# Load existing data or create new
data = {'queue_id': queue_id, 'attempts': []}
if os.path.exists(recovery_file):
    try:
        with open(recovery_file) as f:
            data = json.load(f)
    except Exception:
        pass

# Append new attempt
data['attempts'].append({
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'diagnosis': diagnosis,
    'action': action,
    'details': details
})
data['last_attempt'] = datetime.now(timezone.utc).isoformat()

# Atomic write
with open(tmp_file, 'w') as f:
    json.dump(data, f, indent=2)
os.rename(tmp_file, recovery_file)
" "$queue_id" "$diagnosis" "$action" "$details" "$recovery_file" "$tmp_file" 2>/dev/null

    return $?
}

# is_recoverable(queue_id)
# Returns 0 (true) if attempts < MAX_RECOVERY_ATTEMPTS, 1 (false) otherwise
is_recoverable() {
    local queue_id="$1"
    local attempts
    attempts=$(get_recovery_attempts "$queue_id")

    if [[ "$attempts" -lt "$MAX_RECOVERY_ATTEMPTS" ]]; then
        return 0
    else
        return 1
    fi
}

# get_recovery_json(queue_id)
# Prints the full recovery JSON for a spec (empty object if none)
get_recovery_json() {
    local queue_id="$1"
    local recovery_file="$RECOVERY_DIR/${queue_id}.json"

    if [[ ! -f "$recovery_file" ]]; then
        echo '{}'
        return 0
    fi

    cat "$recovery_file"
}

# append_recovery_report(queue_id, category, diagnosis, action, result)
# Appends a timestamped entry to the recovery report
append_recovery_report() {
    local queue_id="$1"
    local category="$2"
    local diagnosis="$3"
    local action="$4"
    local result="$5"
    local report_file="$HOME/hex/raw/messages/boi-recovery-report.md"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    mkdir -p "$(dirname "$report_file")"
    cat >> "$report_file" <<EOF

## $timestamp — $queue_id
**Category:** $category
**Diagnosis:** $diagnosis
**Action:** $action
**Result:** $result

EOF
}

# notify_recovery_failure(queue_id, message)
# Sends a desktop notification if notify-send is available
notify_recovery_failure() {
    local queue_id="$1"
    local message="${2:-Recovery failed}"
    if command -v notify-send &>/dev/null; then
        notify-send "BOI Recovery" "$queue_id: $message" 2>/dev/null || true
    fi
}

# clear_recovery(queue_id)
# Removes recovery metadata for a spec (use after successful completion)
clear_recovery() {
    local queue_id="$1"
    local recovery_file="$RECOVERY_DIR/${queue_id}.json"

    if [[ -f "$recovery_file" ]]; then
        rm -f "$recovery_file"
    fi
}
