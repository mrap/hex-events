#!/usr/bin/env bash
# boi-auto-recover.sh — Main recovery dispatcher for failed BOI specs
# Called by hex-events policy boi-auto-recovery.yaml
# Dispatches to category-specific recovery sub-scripts
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOI_DB="$HOME/.boi/boi.db"
HEX_EMIT="$HOME/.hex-events/hex_emit.py"
RECOVERY_REPORT="$HOME/hex/raw/messages/boi-recovery-report.md"

# Source recovery utils for attempt tracking
source "$SCRIPT_DIR/boi-recovery-utils.sh"

# --- Args ---
QUEUE_ID="${1:-}"
FAILURE_CATEGORY="${2:-unknown}"

if [[ -z "$QUEUE_ID" ]]; then
    echo "Usage: boi-auto-recover.sh <queue_id> <failure_category>"
    exit 1
fi

echo "[boi-auto-recover] Starting recovery for $QUEUE_ID (category: $FAILURE_CATEGORY)"

# --- Check spec exists in BOI DB ---
if [[ ! -f "$BOI_DB" ]]; then
    echo "[boi-auto-recover] BOI database not found at $BOI_DB"
    exit 1
fi

SPEC_EXISTS=$(python3 -c "
import sqlite3, os, sys
db = sqlite3.connect(os.path.expanduser('~/.boi/boi.db'))
row = db.execute('SELECT id FROM specs WHERE id = ?', (sys.argv[1],)).fetchone()
print('yes' if row else 'no')
db.close()
" "$QUEUE_ID" 2>/dev/null) || true
SPEC_EXISTS="${SPEC_EXISTS:-no}"

if [[ "$SPEC_EXISTS" != "yes" ]]; then
    echo "[boi-auto-recover] Spec not found: $QUEUE_ID"
    exit 1
fi

# --- Check recovery attempts ---
ATTEMPTS=$(get_recovery_attempts "$QUEUE_ID")
echo "[boi-auto-recover] Recovery attempts so far: $ATTEMPTS"

if ! is_recoverable "$QUEUE_ID"; then
    echo "[boi-auto-recover] Max recovery attempts reached ($ATTEMPTS >= $MAX_RECOVERY_ATTEMPTS). Escalating to human."
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"max_recovery_attempts_exceeded\", \"attempts\": $ATTEMPTS, \"category\": \"$FAILURE_CATEGORY\"}" "boi-auto-recover" 2>/dev/null || true
    append_recovery_report "$QUEUE_ID" "$FAILURE_CATEGORY" "Max recovery attempts exceeded" "Escalated to human" "failed"
    notify_recovery_failure "$QUEUE_ID" "Max recovery attempts exceeded ($FAILURE_CATEGORY)"
    exit 1
fi

# --- Dispatch to category-specific recovery script ---
RECOVERY_SCRIPT=""
case "$FAILURE_CATEGORY" in
    max_iterations)
        RECOVERY_SCRIPT="$SCRIPT_DIR/boi-recover-max-iter.sh"
        ;;
    consecutive_failures)
        RECOVERY_SCRIPT="$SCRIPT_DIR/boi-recover-crashes.sh"
        ;;
    death_loop)
        RECOVERY_SCRIPT="$SCRIPT_DIR/boi-recover-death-loop.sh"
        ;;
    *)
        RECOVERY_SCRIPT="$SCRIPT_DIR/boi-recover-investigate.sh"
        ;;
esac

if [[ ! -f "$RECOVERY_SCRIPT" ]]; then
    echo "[boi-auto-recover] Recovery script not found: $RECOVERY_SCRIPT"
    echo "[boi-auto-recover] Falling back to investigation agent"
    RECOVERY_SCRIPT="$SCRIPT_DIR/boi-recover-investigate.sh"
fi

if [[ ! -f "$RECOVERY_SCRIPT" ]]; then
    echo "[boi-auto-recover] Investigation script also not found. Cannot recover."
    record_recovery_attempt "$QUEUE_ID" "$FAILURE_CATEGORY" "no_script" "Recovery script $RECOVERY_SCRIPT not found"
    python3 "$HEX_EMIT" "boi.spec.recovery_failed" "{\"queue_id\": \"$QUEUE_ID\", \"reason\": \"recovery_script_not_found\", \"category\": \"$FAILURE_CATEGORY\"}" "boi-auto-recover" 2>/dev/null || true
    append_recovery_report "$QUEUE_ID" "$FAILURE_CATEGORY" "Recovery script not found" "None" "failed"
    notify_recovery_failure "$QUEUE_ID" "Recovery script not found ($FAILURE_CATEGORY)"
    exit 1
fi

echo "[boi-auto-recover] Dispatching to: $(basename "$RECOVERY_SCRIPT")"
bash "$RECOVERY_SCRIPT" "$QUEUE_ID"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
    echo "[boi-auto-recover] Recovery script exited with code $EXIT_CODE"
fi

exit $EXIT_CODE
