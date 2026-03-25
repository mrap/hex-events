#!/usr/bin/env bash
# morning-briefing.sh — Generate a compact morning briefing text
# Usage: AGENT_DIR=/path/to/agent bash morning-briefing.sh
# Output: formatted text to stdout, suitable for notification
# Never fails overall — missing sources are skipped gracefully

set -uo pipefail

AGENT_DIR="${AGENT_DIR:-$HOME/mrap-hex}"
TODAY="$(date '+%Y-%m-%d')"
TODAY_DOW="$(date '+%A')"
YESTERDAY="$(date -v-1d '+%Y-%m-%d' 2>/dev/null || date -d 'yesterday' '+%Y-%m-%d' 2>/dev/null || echo '')"

# ── 1. Header: date + day of week ────────────────────────────────────────────
printf '=== Morning Briefing: %s, %s ===\n\n' "$TODAY_DOW" "$TODAY"

# ── 2. Calendar ──────────────────────────────────────────────────────────────
if command -v gws >/dev/null 2>&1; then
    printf '📅 Today'\''s Calendar:\n'
    if CAL_OUT=$(gws calendar agenda --today 2>/dev/null); then
        if [[ -n "$CAL_OUT" ]]; then
            printf '%s\n' "$CAL_OUT"
        else
            printf '  (no events)\n'
        fi
    else
        printf '  (calendar unavailable)\n'
    fi
    printf '\n'
fi

# ── 3. Yesterday's landings ───────────────────────────────────────────────────
printf '📋 Yesterday'\''s Landings'
if [[ -n "$YESTERDAY" ]]; then
    YEST_FILE="${AGENT_DIR}/landings/${YESTERDAY}.md"
    if [[ -f "$YEST_FILE" ]]; then
        YEST_DONE=$(grep -cE '^\s*-\s*\[x\]' "$YEST_FILE" 2>/dev/null || echo 0)
        YEST_TOTAL=$(grep -cE '^\s*-\s*\[' "$YEST_FILE" 2>/dev/null || echo 0)
        printf ' (%s): %d/%d done\n' "$YESTERDAY" "$YEST_DONE" "$YEST_TOTAL"
    else
        printf ' (%s): no file\n' "$YESTERDAY"
    fi
else
    printf ': (date unavailable)\n'
fi

# ── 4. Today's landings ───────────────────────────────────────────────────────
printf '📋 Today'\''s Landings'
TODAY_FILE="${AGENT_DIR}/landings/${TODAY}.md"
if [[ -f "$TODAY_FILE" ]]; then
    TODAY_DONE=$(grep -cE '^\s*-\s*\[x\]' "$TODAY_FILE" 2>/dev/null || echo 0)
    TODAY_TOTAL=$(grep -cE '^\s*-\s*\[' "$TODAY_FILE" 2>/dev/null || echo 0)
    printf ': %d/%d done\n' "$TODAY_DONE" "$TODAY_TOTAL"
else
    printf ': no file yet\n'
fi
printf '\n'

# ── 5. BOI overnight ─────────────────────────────────────────────────────────
printf '🤖 BOI Overnight:\n'
if BOI_RAW=$(bash ~/.boi/boi status 2>/dev/null); then
    BOI_COMPLETED=$(printf '%s' "$BOI_RAW" | grep -c 'completed' 2>/dev/null || echo 0)
    BOI_FAILED=$(printf '%s' "$BOI_RAW" | grep -c 'failed' 2>/dev/null || echo 0)
    BOI_RUNNING=$(printf '%s' "$BOI_RAW" | grep -c 'running' 2>/dev/null || echo 0)
    printf '  completed: %d  failed: %d  running: %d\n' "$BOI_COMPLETED" "$BOI_FAILED" "$BOI_RUNNING"
else
    printf '  (boi status unavailable)\n'
fi
printf '\n'

# ── 6. Unread messages ────────────────────────────────────────────────────────
printf '💬 Messages:\n'
if command -v imsg >/dev/null 2>&1; then
    if IMSG_RAW=$(imsg unread 2>/dev/null); then
        UNREAD=$(printf '%s' "$IMSG_RAW" | grep -c '.' 2>/dev/null || echo 0)
        printf '  unread: %d\n' "$UNREAD"
    else
        printf '  (imsg unavailable)\n'
    fi
else
    printf '  (imsg not installed)\n'
fi
printf '\n'

# ── 7. hex-events health ──────────────────────────────────────────────────────
printf '⚙️  hex-events:\n'
if HEX_RAW=$(python3 ~/.hex-events/hex_events_cli.py status 2>/dev/null); then
    if printf '%s' "$HEX_RAW" | grep -qiE 'running|ok|active'; then
        printf '  status: ok\n'
    else
        printf '  status: degraded\n'
    fi
    # Show first 3 lines of status for detail
    printf '%s\n' "$HEX_RAW" | head -3 | sed 's/^/  /'
else
    printf '  status: unavailable\n'
fi
printf '\n'

# ── 8. Top 3 priorities ───────────────────────────────────────────────────────
PRIORITY_FILE="${AGENT_DIR}/evolution/priority-ranked.yaml"
printf '🎯 Top Priorities:\n'
if [[ -f "$PRIORITY_FILE" ]]; then
    # Extract first 3 non-empty, non-comment, non-header lines that look like list items
    COUNT=0
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^--- ]] && continue
        # Match lines that start with - or are numbered items or contain meaningful text
        if [[ "$line" =~ ^[[:space:]]*- ]] || [[ "$line" =~ ^[[:space:]]*[0-9]+\. ]]; then
            printf '  %s\n' "${line#"${line%%[![:space:]]*}"}"
            COUNT=$((COUNT + 1))
            [[ $COUNT -ge 3 ]] && break
        fi
    done < "$PRIORITY_FILE"
    if [[ $COUNT -eq 0 ]]; then
        printf '  (no priorities found in file)\n'
    fi
else
    printf '  (no priority file at %s)\n' "$PRIORITY_FILE"
fi
