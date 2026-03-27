#!/usr/bin/env bash
# boi-recover-max-iter.sh — Recovery script for specs that hit max iterations
# Called by boi-auto-recover.sh when failure_category=max_iterations
#
# Logic:
# - If last 3 iterations each completed >= 1 task: spec is making progress
#   -> Bump max_iterations by 50%, resume
# - If last 3 iterations completed 0 tasks: stuck
#   -> Invoke investigation agent
# - If mixed results: ambiguous
#   -> Invoke investigation agent
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOI_DB="$HOME/.boi/boi.db"
BOI_CMD="$HOME/boi/boi.sh"
HEX_EMIT="$HOME/.hex-events/hex_emit.py"
RECOVERY_REPORT="$HOME/hex/raw/messages/boi-recovery-report.md"

# Source recovery utils
source "$SCRIPT_DIR/boi-recovery-utils.sh"

QUEUE_ID="${1:-}"
if [[ -z "$QUEUE_ID" ]]; then
    echo "[boi-recover-max-iter] Usage: boi-recover-max-iter.sh <queue_id>"
    exit 1
fi

echo "[boi-recover-max-iter] Analyzing iteration history for $QUEUE_ID"

# --- Get last 3 iterations' tasks_completed from DB ---
ITER_DATA=$(_RECOVER_QID="$QUEUE_ID" python3 << 'PYEOF'
import sqlite3, os, json

queue_id = os.environ.get('_RECOVER_QID', '')
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
rows = db.execute(
    'SELECT iteration, tasks_completed FROM iterations WHERE spec_id = ? ORDER BY iteration DESC LIMIT 3',
    (queue_id,)
).fetchall()
db.close()

if not rows:
    print(json.dumps({'count': 0, 'tasks': []}))
else:
    rows.reverse()
    print(json.dumps({
        'count': len(rows),
        'tasks': [r[1] or 0 for r in rows]
    }))
PYEOF
) || true
if [[ -z "${ITER_DATA:-}" ]]; then
    ITER_DATA='{"count":0,"tasks":[]}'
fi

ITER_COUNT=$(echo "$ITER_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['count'])" 2>/dev/null) || true
ITER_COUNT="${ITER_COUNT:-0}"

if [[ "$ITER_COUNT" -eq 0 ]]; then
    # Fallback: try telemetry file
    TELEMETRY_FILE="$HOME/.boi/queue/${QUEUE_ID}.telemetry.json"
    if [[ -f "$TELEMETRY_FILE" ]]; then
        ITER_DATA=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    t = json.load(f)
tasks = t.get('tasks_completed_per_iteration', [])
last3 = tasks[-3:] if len(tasks) >= 3 else tasks
print(json.dumps({'count': len(last3), 'tasks': last3}))
" "$TELEMETRY_FILE" 2>/dev/null) || true
        if [[ -z "${ITER_DATA:-}" ]]; then
    ITER_DATA='{"count":0,"tasks":[]}'
fi
        ITER_COUNT=$(echo "$ITER_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['count'])" 2>/dev/null) || true
        ITER_COUNT="${ITER_COUNT:-0}"
    fi
fi

echo "[boi-recover-max-iter] Found $ITER_COUNT recent iterations"

if [[ "$ITER_COUNT" -eq 0 ]]; then
    echo "[boi-recover-max-iter] No iteration data found. Invoking investigation agent."
    if [[ -f "$SCRIPT_DIR/boi-recover-investigate.sh" ]]; then
        bash "$SCRIPT_DIR/boi-recover-investigate.sh" "$QUEUE_ID"
        exit $?
    else
        record_recovery_attempt "$QUEUE_ID" "max_iterations" "no_data" "No iteration data and no investigation script"
        echo "[boi-recover-max-iter] Investigation script not available. Cannot recover."
        exit 1
    fi
fi

# --- Analyze progress pattern ---
TASKS_JSON=$(echo "$ITER_DATA" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['tasks']))" 2>/dev/null) || true
echo "[boi-recover-max-iter] Last iterations tasks_completed: $TASKS_JSON"

# Check if all last 3 completed >= 1 task (making progress)
ALL_PROGRESS=$(echo "$ITER_DATA" | python3 -c "
import json, sys
d = json.load(sys.stdin)
tasks = d['tasks']
# All must have completed >= 1 task
print('yes' if all(t >= 1 for t in tasks) and len(tasks) >= 3 else 'no')
" 2>/dev/null) || true
ALL_PROGRESS="${ALL_PROGRESS:-no}"

# Check if all last 3 completed 0 tasks (stuck)
ALL_STUCK=$(echo "$ITER_DATA" | python3 -c "
import json, sys
d = json.load(sys.stdin)
tasks = d['tasks']
print('yes' if all(t == 0 for t in tasks) and len(tasks) >= 3 else 'no')
" 2>/dev/null) || true
ALL_STUCK="${ALL_STUCK:-no}"

# --- Act on analysis ---
append_report() {
    local diagnosis="$1"
    local action="$2"
    local result="$3"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    mkdir -p "$(dirname "$RECOVERY_REPORT")"
    cat >> "$RECOVERY_REPORT" <<EOF

## $timestamp — $QUEUE_ID
**Category:** max_iterations
**Diagnosis:** $diagnosis
**Action:** $action
**Result:** $result

EOF
}

if [[ "$ALL_PROGRESS" == "yes" ]]; then
    echo "[boi-recover-max-iter] Spec is making progress (all last 3 iterations completed tasks). Bumping iterations."

    # Get current max_iterations and bump by 50%
    CURRENT_MAX=$(python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
row = db.execute('SELECT max_iterations FROM specs WHERE id = ?', (sys.argv[1],)).fetchone()
db.close()
print(row[0] if row else 30)
" "$QUEUE_ID" 2>/dev/null) || true
    CURRENT_MAX="${CURRENT_MAX:-30}"

    NEW_MAX=$(python3 -c "import sys, math; print(math.ceil(int(sys.argv[1]) * 1.5))" "$CURRENT_MAX" 2>/dev/null) || true
    NEW_MAX="${NEW_MAX:-45}"

    echo "[boi-recover-max-iter] Bumping max_iterations: $CURRENT_MAX -> $NEW_MAX"

    # Update max_iterations in DB
    python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute('UPDATE specs SET max_iterations = ? WHERE id = ?', (int(sys.argv[2]), sys.argv[1]))
db.commit()
db.close()
" "$QUEUE_ID" "$NEW_MAX" 2>/dev/null
    UPDATE_RC=$?

    if [[ $UPDATE_RC -ne 0 ]]; then
        echo "[boi-recover-max-iter] Failed to update max_iterations in DB"
        record_recovery_attempt "$QUEUE_ID" "max_iterations" "bump_failed" "Failed to update DB"
        append_report "Making progress, bumped iterations" "bump $CURRENT_MAX -> $NEW_MAX" "failed"
        notify_recovery_failure "$QUEUE_ID" "Bump iterations failed (DB error)"
        exit 1
    fi

    # Resume the spec
    echo "[boi-recover-max-iter] Resuming spec..."
    bash "$BOI_CMD" resume "$QUEUE_ID" 2>/dev/null
    RESUME_RC=$?

    if [[ $RESUME_RC -ne 0 ]]; then
        echo "[boi-recover-max-iter] boi resume failed (exit $RESUME_RC). Trying direct DB update."
        python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute(\"UPDATE specs SET status = 'queued', failure_reason = NULL WHERE id = ?\", (sys.argv[1],))
db.commit()
db.close()
" "$QUEUE_ID" 2>/dev/null || true
    fi

    # Record success
    record_recovery_attempt "$QUEUE_ID" "max_iterations" "bump_iterations" "Bumped $CURRENT_MAX -> $NEW_MAX, resumed"
    python3 "$HEX_EMIT" "boi.spec.recovered" "{\"queue_id\": \"$QUEUE_ID\", \"action\": \"bump_iterations\", \"old_max\": $CURRENT_MAX, \"new_max\": $NEW_MAX}" "boi-auto-recover" 2>/dev/null || true
    append_report "Making progress (all last 3 iters completed tasks)" "Bumped iterations $CURRENT_MAX -> $NEW_MAX, resumed" "success"
    echo "[boi-recover-max-iter] Recovery complete. Spec resumed with $NEW_MAX max iterations."
    exit 0

elif [[ "$ALL_STUCK" == "yes" ]]; then
    echo "[boi-recover-max-iter] Spec is stuck (last 3 iterations completed 0 tasks). Invoking investigation agent."
    if [[ -f "$SCRIPT_DIR/boi-recover-investigate.sh" ]]; then
        bash "$SCRIPT_DIR/boi-recover-investigate.sh" "$QUEUE_ID"
        exit $?
    else
        record_recovery_attempt "$QUEUE_ID" "max_iterations" "stuck_no_investigator" "All last 3 iterations completed 0 tasks, investigation agent not available"
        append_report "Stuck (0 tasks in last 3 iterations)" "Investigation agent not available" "failed"
        python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"stuck_no_investigator\", \"category\": \"max_iterations\"}" "boi-auto-recover" 2>/dev/null || true
        notify_recovery_failure "$QUEUE_ID" "Stuck (0 tasks in last 3 iters), no investigator"
        exit 1
    fi

else
    echo "[boi-recover-max-iter] Mixed progress pattern. Invoking investigation agent."
    if [[ -f "$SCRIPT_DIR/boi-recover-investigate.sh" ]]; then
        bash "$SCRIPT_DIR/boi-recover-investigate.sh" "$QUEUE_ID"
        exit $?
    else
        record_recovery_attempt "$QUEUE_ID" "max_iterations" "mixed_no_investigator" "Mixed progress pattern, investigation agent not available"
        append_report "Mixed progress (some iterations productive, some not)" "Investigation agent not available" "failed"
        python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"mixed_no_investigator\", \"category\": \"max_iterations\"}" "boi-auto-recover" 2>/dev/null || true
        notify_recovery_failure "$QUEUE_ID" "Mixed progress, no investigator"
        exit 1
    fi
fi
