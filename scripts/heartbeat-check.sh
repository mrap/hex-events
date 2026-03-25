#!/usr/bin/env bash
# heartbeat-check.sh — Gather system state and output compact JSON summary
# Usage: bash heartbeat-check.sh
# Output: JSON to stdout
# Never fails overall — missing sources produce null fields

set -uo pipefail

AGENT_DIR="${AGENT_DIR:-$HOME/mrap-hex}"
TIMESTAMP="$(date -u '+%Y-%m-%dT%H:%M:%S')"

# Safe grep count: always returns a plain integer, never fails
_gcount() { grep -cE "$1" "$2" 2>/dev/null; true; }
_gcount_i() { grep -ciE "$1" "$2" 2>/dev/null; true; }
_pcount() { printf '%s' "$1" | grep -cE "$2" 2>/dev/null; true; }

# ── 1. BOI queue status ─────────────────────────────────────────────────────
BOI_JSON="null"
if BOI_RAW=$(bash ~/.boi/boi status 2>/dev/null); then
    BOI_RUNNING=$(_pcount "$BOI_RAW" 'running')
    BOI_FAILED=$(_pcount "$BOI_RAW" 'failed')
    BOI_JSON="{\"running\":${BOI_RUNNING:-0},\"failed\":${BOI_FAILED:-0}}"
fi

# ── 2. iMessage unread count ────────────────────────────────────────────────
UNREAD_MSGS="null"
if command -v imsg >/dev/null 2>&1; then
    if IMSG_RAW=$(imsg unread 2>/dev/null); then
        UNREAD_MSGS=$(_pcount "$IMSG_RAW" '.')
        UNREAD_MSGS="${UNREAD_MSGS:-0}"
    fi
fi

# ── 3. Landings: count done/total items ─────────────────────────────────────
LANDINGS_JSON="null"
THREADS="null"
TODAY="$(date '+%Y-%m-%d')"
LANDINGS_FILE="${AGENT_DIR}/landings/${TODAY}.md"
if [[ -f "$LANDINGS_FILE" ]]; then
    DONE_COUNT=$(_gcount '^\s*-\s*\[x\]' "$LANDINGS_FILE")
    TOTAL_COUNT=$(_gcount '^\s*-\s*\[' "$LANDINGS_FILE")
    LANDINGS_JSON="{\"done\":${DONE_COUNT:-0},\"total\":${TOTAL_COUNT:-0}}"
    THREADS=$(_gcount_i 'thread|follow.up|waiting' "$LANDINGS_FILE")
    THREADS="${THREADS:-0}"
fi

# ── 4. hex-events health ─────────────────────────────────────────────────────
EVENTS_HEALTH="null"
if HEX_RAW=$(python3 ~/.hex-events/hex_events_cli.py status 2>/dev/null); then
    if printf '%s' "$HEX_RAW" | grep -qiE 'running|ok|active'; then
        EVENTS_HEALTH='"ok"'
    else
        EVENTS_HEALTH='"degraded"'
    fi
fi

# ── Output JSON ──────────────────────────────────────────────────────────────
printf '{"timestamp":"%s","boi":%s,"landings":%s,"threads":%s,"unread_msgs":%s,"events_health":%s}\n' \
    "$TIMESTAMP" \
    "$BOI_JSON" \
    "$LANDINGS_JSON" \
    "$THREADS" \
    "$UNREAD_MSGS" \
    "$EVENTS_HEALTH"
