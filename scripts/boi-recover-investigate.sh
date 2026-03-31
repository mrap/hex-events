#!/usr/bin/env bash
# boi-recover-investigate.sh — Investigation agent for ambiguous BOI spec failures
# Reads spec content, iteration logs, and telemetry. Runs a Claude investigation
# agent that outputs a JSON recommendation. Applies the recommendation if safe.
#
# Called by other recovery scripts (max-iter, crashes) when diagnosis is unclear,
# or directly by boi-auto-recover.sh for unknown failure categories.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOI_DB="$HOME/.boi/boi.db"
BOI_CMD="$HOME/boi/boi.sh"
HEX_EMIT="$HOME/.hex-events/hex_emit.py"
RECOVERY_REPORT="$HOME/hex/raw/messages/boi-recovery-report.md"
TEMPLATE_FILE="$HOME/.hex-events/templates/boi-investigation.md"

# Source recovery utils
source "$SCRIPT_DIR/boi-recovery-utils.sh"

QUEUE_ID="${1:-}"
FAILURE_CATEGORY="${2:-unknown}"

if [[ -z "$QUEUE_ID" ]]; then
    echo "[boi-recover-investigate] Usage: boi-recover-investigate.sh <queue_id> [failure_category]"
    exit 1
fi

echo "[boi-recover-investigate] Starting investigation for $QUEUE_ID (category: $FAILURE_CATEGORY)"

# --- Helper: append to recovery report ---
append_report() {
    local diagnosis="$1"
    local action="$2"
    local result="$3"
    local timestamp
    timestamp=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    mkdir -p "$(dirname "$RECOVERY_REPORT")"
    cat >> "$RECOVERY_REPORT" <<EOF

## $timestamp — $QUEUE_ID
**Category:** $FAILURE_CATEGORY (investigation)
**Diagnosis:** $diagnosis
**Action:** $action
**Result:** $result

EOF
}

# --- Check template exists ---
if [[ ! -f "$TEMPLATE_FILE" ]]; then
    echo "[boi-recover-investigate] Template not found: $TEMPLATE_FILE"
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_no_template" "Investigation template missing"
    append_report "Investigation template missing" "None" "failed"
    notify_recovery_failure "$QUEUE_ID" "Investigation template missing"
    exit 1
fi

# --- Check recovery attempts ---
ATTEMPTS=$(get_recovery_attempts "$QUEUE_ID")
if ! is_recoverable "$QUEUE_ID"; then
    echo "[boi-recover-investigate] Max recovery attempts reached ($ATTEMPTS). Escalating."
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"max_recovery_attempts_exceeded\", \"attempts\": $ATTEMPTS}" "boi-auto-recover" 2>/dev/null || true
    append_report "Max recovery attempts exceeded" "Escalated to human" "failed"
    notify_recovery_failure "$QUEUE_ID" "Max recovery attempts exceeded"
    exit 1
fi

# --- Gather context for the investigation agent ---

# 1. Read spec content
SPEC_PATH=$(_RECOVER_QID="$QUEUE_ID" python3 -c "
import sqlite3, os
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
row = db.execute('SELECT spec_path FROM specs WHERE id = ?', (os.environ['_RECOVER_QID'],)).fetchone()
db.close()
print(row[0] if row else '')
" 2>/dev/null) || true

SPEC_CONTENT=""
if [[ -n "$SPEC_PATH" ]] && [[ -f "$SPEC_PATH" ]]; then
    # Truncate to first 3000 chars to keep prompt manageable
    SPEC_CONTENT=$(head -c 3000 "$SPEC_PATH" 2>/dev/null) || true
fi
if [[ -z "$SPEC_CONTENT" ]]; then
    SPEC_CONTENT="(Spec file not found or empty at: ${SPEC_PATH:-unknown})"
fi

# 2. Read last 5 iteration logs from DB
ITERATION_LOGS=$(_RECOVER_QID="$QUEUE_ID" python3 << 'PYEOF'
import sqlite3, os, json

queue_id = os.environ.get('_RECOVER_QID', '')
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))

rows = db.execute(
    'SELECT iteration, worker_id, started_at, ended_at, duration_seconds, '
    'tasks_completed, tasks_added, tasks_skipped, exit_code, pre_pending, post_pending '
    'FROM iterations WHERE spec_id = ? ORDER BY iteration DESC LIMIT 5',
    (queue_id,)
).fetchall()

if not rows:
    print('(No iteration data found)')
else:
    rows.reverse()
    for r in rows:
        print(f'Iteration {r[0]}: worker={r[1]}, started={r[2]}, ended={r[3]}, '
              f'duration={r[4]}s, tasks_done={r[5]}, tasks_added={r[6]}, '
              f'tasks_skipped={r[7]}, exit_code={r[8]}, '
              f'pre_pending={r[9]}, post_pending={r[10]}')

# Also get recent events for error context
events = db.execute(
    "SELECT timestamp, event_type, message FROM events "
    "WHERE spec_id = ? AND level IN ('error', 'warning') "
    "ORDER BY seq DESC LIMIT 10",
    (queue_id,)
).fetchall()

if events:
    print('\nRecent errors/warnings:')
    for e in reversed(events):
        print(f'  [{e[0]}] {e[1]}: {e[2]}')

db.close()
PYEOF
) || true
if [[ -z "${ITERATION_LOGS:-}" ]]; then
    ITERATION_LOGS="(No iteration data available)"
fi

# 3. Read telemetry
TELEMETRY="{}"
TELEMETRY_FILE="$HOME/.boi/queue/${QUEUE_ID}.telemetry.json"
if [[ -f "$TELEMETRY_FILE" ]]; then
    # Truncate to 2000 chars
    TELEMETRY=$(head -c 2000 "$TELEMETRY_FILE" 2>/dev/null) || true
fi

# Also get spec metadata from DB
SPEC_META=$(_RECOVER_QID="$QUEUE_ID" python3 -c "
import sqlite3, os, json
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
row = db.execute(
    'SELECT id, status, iteration, max_iterations, tasks_done, tasks_total, '
    'consecutive_failures, environment, failure_reason, last_worker '
    'FROM specs WHERE id = ?',
    (os.environ['_RECOVER_QID'],)
).fetchone()
db.close()
if row:
    print(json.dumps({
        'id': row[0], 'status': row[1], 'iteration': row[2],
        'max_iterations': row[3], 'tasks_done': row[4], 'tasks_total': row[5],
        'consecutive_failures': row[6], 'environment': row[7],
        'failure_reason': row[8], 'last_worker': row[9]
    }))
else:
    print('{}')
" 2>/dev/null) || true
SPEC_META="${SPEC_META:-{}}"

# Combine telemetry with spec metadata
COMBINED_TELEMETRY=$(python3 -c "
import json, sys
meta = json.loads(sys.argv[1])
telem = {}
try:
    telem = json.loads(sys.argv[2])
except Exception:
    pass
combined = {**telem, 'spec_metadata': meta}
print(json.dumps(combined, indent=2))
" "$SPEC_META" "$TELEMETRY" 2>/dev/null) || true
COMBINED_TELEMETRY="${COMBINED_TELEMETRY:-$TELEMETRY}"

# 4. Recovery attempts so far
RECOVERY_JSON=$(get_recovery_json "$QUEUE_ID")

# --- Build the prompt ---
echo "[boi-recover-investigate] Building investigation prompt..."

PROMPT_CONTENT=$(cat "$TEMPLATE_FILE")

# Replace placeholders
PROMPT_CONTENT="${PROMPT_CONTENT//\{\{SPEC_CONTENT\}\}/$SPEC_CONTENT}"
PROMPT_CONTENT="${PROMPT_CONTENT//\{\{ITERATION_LOGS\}\}/$ITERATION_LOGS}"
PROMPT_CONTENT="${PROMPT_CONTENT//\{\{TELEMETRY\}\}/$COMBINED_TELEMETRY}"
PROMPT_CONTENT="${PROMPT_CONTENT//\{\{FAILURE_CATEGORY\}\}/$FAILURE_CATEGORY}"
PROMPT_CONTENT="${PROMPT_CONTENT//\{\{RECOVERY_ATTEMPTS\}\}/$RECOVERY_JSON}"

# Add turn limit instruction (standing order #37: no timeout command)
PROMPT_CONTENT="$PROMPT_CONTENT

IMPORTANT: Respond in a single turn. Do not use any tools. Output ONLY the JSON line."

# --- Run Claude investigation agent ---
echo "[boi-recover-investigate] Running Claude investigation agent..."

# Write prompt to temp file to avoid shell escaping issues
PROMPT_TMP=$(mktemp /tmp/boi-investigate-prompt-XXXXXX.md)
echo "$PROMPT_CONTENT" > "$PROMPT_TMP"

AGENT_OUTPUT=""
ERR_LOG="/tmp/boi-recover-investigate-${QUEUE_ID}.err.log"
AGENT_OUTPUT=$(env -u CLAUDECODE /Users/mrap/.local/bin/claude --dangerously-skip-permissions --print --no-session-persistence --max-turns 1 -p "$(cat "$PROMPT_TMP")" </dev/null 2>>"$ERR_LOG") || true

rm -f "$PROMPT_TMP"

if [[ -z "$AGENT_OUTPUT" ]]; then
    echo "[boi-recover-investigate] Claude agent returned empty output"
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_empty" "Claude investigation agent returned no output"
    append_report "Investigation agent returned empty output" "Escalated to human" "failed"
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"investigation_empty_output\", \"category\": \"$FAILURE_CATEGORY\"}" "boi-auto-recover" 2>/dev/null || true
    notify_recovery_failure "$QUEUE_ID" "Investigation agent returned empty output"
    exit 1
fi

echo "[boi-recover-investigate] Agent output: $AGENT_OUTPUT"

# --- Parse JSON output ---
PARSED=$(_AGENT_OUT="$AGENT_OUTPUT" python3 << 'PYEOF'
import json, os, re

raw = os.environ.get('_AGENT_OUT', '')

# Try to extract JSON from output (agent might include extra text)
parsed = None

# Try direct parse first
try:
    parsed = json.loads(raw.strip())
except Exception:
    pass

# Try to find JSON object in the output
if parsed is None:
    match = re.search(r'\{[^{}]*"diagnosis"[^{}]*"action"[^{}]*\}', raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
        except Exception:
            pass

# Try line by line
if parsed is None:
    for line in raw.strip().split('\n'):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                parsed = json.loads(line)
                if 'action' in parsed:
                    break
            except Exception:
                continue

if parsed and 'action' in parsed:
    # Ensure required fields
    parsed.setdefault('diagnosis', 'unknown')
    parsed.setdefault('confidence', 0.5)
    parsed.setdefault('details', {})
    parsed.setdefault('reasoning', '')
    print(json.dumps(parsed))
else:
    print(json.dumps({'error': 'parse_failed', 'raw': raw[:500]}))
PYEOF
) || true

if [[ -z "${PARSED:-}" ]]; then
    echo "[boi-recover-investigate] Failed to parse agent output"
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_parse_fail" "Could not parse Claude output"
    append_report "Investigation agent output unparseable" "Escalated to human" "failed"
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"investigation_parse_failed\", \"category\": \"$FAILURE_CATEGORY\"}" "boi-auto-recover" 2>/dev/null || true
    notify_recovery_failure "$QUEUE_ID" "Investigation output unparseable"
    exit 1
fi

# Check for parse error
PARSE_ERROR=$(echo "$PARSED" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error',''))" 2>/dev/null) || true
if [[ "$PARSE_ERROR" == "parse_failed" ]]; then
    echo "[boi-recover-investigate] Agent output did not contain valid JSON recommendation"
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_bad_json" "Agent output not valid JSON"
    append_report "Investigation agent output not valid JSON" "Escalated to human" "failed"
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"investigation_bad_json\", \"category\": \"$FAILURE_CATEGORY\"}" "boi-auto-recover" 2>/dev/null || true
    notify_recovery_failure "$QUEUE_ID" "Investigation output not valid JSON"
    exit 1
fi

# Extract fields
ACTION=$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin)['action'])" 2>/dev/null) || true
DIAGNOSIS=$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin)['diagnosis'])" 2>/dev/null) || true
CONFIDENCE=$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin)['confidence'])" 2>/dev/null) || true
DETAILS=$(echo "$PARSED" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('details',{})))" 2>/dev/null) || true
REASONING=$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reasoning',''))" 2>/dev/null) || true

echo "[boi-recover-investigate] Diagnosis: $DIAGNOSIS"
echo "[boi-recover-investigate] Action: $ACTION (confidence: $CONFIDENCE)"
echo "[boi-recover-investigate] Reasoning: $REASONING"

# --- Confidence check: if < 0.6, always escalate ---
LOW_CONFIDENCE=$(_CONF="${CONFIDENCE:-0}" python3 -c "import os; print('yes' if float(os.environ.get('_CONF','0')) < 0.6 else 'no')" 2>/dev/null) || true
if [[ "$LOW_CONFIDENCE" == "yes" ]]; then
    echo "[boi-recover-investigate] Confidence too low ($CONFIDENCE < 0.6). Escalating to human."
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_low_confidence" "Diagnosis: $DIAGNOSIS. Confidence $CONFIDENCE < 0.6 threshold."
    append_report "Investigation: $DIAGNOSIS (low confidence $CONFIDENCE)" "Escalated to human (confidence < 0.6)" "failed"
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"low_confidence\", \"confidence\": $CONFIDENCE, \"diagnosis\": \"$DIAGNOSIS\", \"action\": \"$ACTION\"}" "boi-auto-recover" 2>/dev/null || true
    notify_recovery_failure "$QUEUE_ID" "Low confidence ($CONFIDENCE): $DIAGNOSIS"
    exit 1
fi

# --- Apply the recommendation ---
case "$ACTION" in
    bump_iterations)
        SUGGESTED_MAX=$(echo "$DETAILS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('suggested_max', 45))" 2>/dev/null) || true
        SUGGESTED_MAX="${SUGGESTED_MAX:-45}"

        echo "[boi-recover-investigate] Applying: bump max_iterations to $SUGGESTED_MAX"
        python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute('UPDATE specs SET max_iterations = ? WHERE id = ?', (int(sys.argv[2]), sys.argv[1]))
db.commit()
db.close()
" "$QUEUE_ID" "$SUGGESTED_MAX" 2>/dev/null
        UPDATE_RC=$?

        if [[ $UPDATE_RC -ne 0 ]]; then
            record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_bump_failed" "DB update failed for bump_iterations"
            append_report "Investigation: $DIAGNOSIS" "Bump iterations failed (DB error)" "failed"
            exit 1
        fi

        bash "$BOI_CMD" resume "$QUEUE_ID" 2>/dev/null
        RESUME_RC=$?
        if [[ $RESUME_RC -ne 0 ]]; then
            python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute(\"UPDATE specs SET status = 'queued', failure_reason = NULL WHERE id = ?\", (sys.argv[1],))
db.commit()
db.close()
" "$QUEUE_ID" 2>/dev/null || true
        fi

        record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_bump" "Investigation recommended bump to $SUGGESTED_MAX. Diagnosis: $DIAGNOSIS"
        python3 "$HEX_EMIT" "boi.spec.recovered" "{\"queue_id\": \"$QUEUE_ID\", \"action\": \"bump_iterations\", \"new_max\": $SUGGESTED_MAX, \"confidence\": $CONFIDENCE}" "boi-auto-recover" 2>/dev/null || true
        append_report "Investigation: $DIAGNOSIS" "Bumped iterations to $SUGGESTED_MAX, resumed" "success"
        echo "[boi-recover-investigate] Recovery complete. Bumped to $SUGGESTED_MAX iterations."
        exit 0
        ;;

    skip_task)
        SKIP_TASK_ID=$(echo "$DETAILS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('task_id', ''))" 2>/dev/null) || true
        SKIP_REASON=$(echo "$DETAILS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason', 'Investigation agent recommended skip'))" 2>/dev/null) || true

        if [[ -z "$SKIP_TASK_ID" ]]; then
            echo "[boi-recover-investigate] skip_task recommended but no task_id provided. Escalating."
            record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_skip_no_id" "Skip recommended but no task_id"
            append_report "Investigation: $DIAGNOSIS" "Skip recommended but no task_id" "failed"
            exit 1
        fi

        echo "[boi-recover-investigate] Applying: skip task $SKIP_TASK_ID"

        # Mark task as SKIPPED in the spec file
        if [[ -n "$SPEC_PATH" ]] && [[ -f "$SPEC_PATH" ]]; then
            # Find the task line and change PENDING to SKIPPED
            sed -i "s/^### $SKIP_TASK_ID:.*$/&/; /^### $SKIP_TASK_ID:/{ n; s/PENDING/SKIPPED (auto-recovery: $SKIP_REASON)/ }" "$SPEC_PATH" 2>/dev/null || true
        fi

        # Resume the spec
        bash "$BOI_CMD" resume "$QUEUE_ID" 2>/dev/null
        RESUME_RC=$?
        if [[ $RESUME_RC -ne 0 ]]; then
            python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute(\"UPDATE specs SET status = 'queued', failure_reason = NULL WHERE id = ?\", (sys.argv[1],))
db.commit()
db.close()
" "$QUEUE_ID" 2>/dev/null || true
        fi

        record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_skip" "Skipped task $SKIP_TASK_ID: $SKIP_REASON. Diagnosis: $DIAGNOSIS"
        python3 "$HEX_EMIT" "boi.spec.recovered" "{\"queue_id\": \"$QUEUE_ID\", \"action\": \"skip_task\", \"task_id\": \"$SKIP_TASK_ID\", \"confidence\": $CONFIDENCE}" "boi-auto-recover" 2>/dev/null || true
        append_report "Investigation: $DIAGNOSIS" "Skipped task $SKIP_TASK_ID, resumed" "success"
        echo "[boi-recover-investigate] Recovery complete. Skipped $SKIP_TASK_ID."
        exit 0
        ;;

    re_route)
        TARGET_ENV=$(echo "$DETAILS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('target_environment', 'local'))" 2>/dev/null) || true
        TARGET_ENV="${TARGET_ENV:-local}"

        echo "[boi-recover-investigate] Applying: re-route environment to $TARGET_ENV"
        python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
db.execute('UPDATE specs SET environment = ?, status = ? WHERE id = ?', (sys.argv[2], 'queued', sys.argv[1]))
db.commit()
db.close()
" "$QUEUE_ID" "$TARGET_ENV" 2>/dev/null
        REROUTE_RC=$?

        if [[ $REROUTE_RC -ne 0 ]]; then
            record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_reroute_failed" "DB update failed for re_route"
            append_report "Investigation: $DIAGNOSIS" "Re-route to $TARGET_ENV failed (DB error)" "failed"
            exit 1
        fi

        record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_reroute" "Re-routed to $TARGET_ENV. Diagnosis: $DIAGNOSIS"
        python3 "$HEX_EMIT" "boi.spec.recovered" "{\"queue_id\": \"$QUEUE_ID\", \"action\": \"re_route\", \"target_environment\": \"$TARGET_ENV\", \"confidence\": $CONFIDENCE}" "boi-auto-recover" 2>/dev/null || true
        append_report "Investigation: $DIAGNOSIS" "Re-routed to $TARGET_ENV, requeued" "success"
        echo "[boi-recover-investigate] Recovery complete. Re-routed to $TARGET_ENV."
        exit 0
        ;;

    needs_human|simplify_spec|*)
        # Cannot auto-fix. Escalate to human.
        BLOCKER=$(echo "$DETAILS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('blocker', d.get('suggestion', 'See diagnosis')))" 2>/dev/null) || true
        BLOCKER="${BLOCKER:-See diagnosis}"

        echo "[boi-recover-investigate] Action requires human: $ACTION"
        echo "[boi-recover-investigate] Blocker: $BLOCKER"

        record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "investigate_$ACTION" "Diagnosis: $DIAGNOSIS. Blocker: $BLOCKER"
        python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"$ACTION\", \"diagnosis\": \"$DIAGNOSIS\", \"blocker\": \"$BLOCKER\", \"confidence\": $CONFIDENCE}" "boi-auto-recover" 2>/dev/null || true
        append_report "Investigation: $DIAGNOSIS" "$ACTION: $BLOCKER" "failed"
        notify_recovery_failure "$QUEUE_ID" "Needs human: $DIAGNOSIS"
        exit 1
        ;;
esac
