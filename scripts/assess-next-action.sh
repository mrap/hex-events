#!/usr/bin/env bash
# assess-next-action.sh — After a spec completes, assess what needs to happen next
# Generic: works with any orchestrator (BOI or custom) that uses hex-events
#
# Usage: assess-next-action.sh <spec_id> [spec_title] [target_repo]
#
# Environment:
#   AGENT_DIR          — hex agent directory (for capture output)
#   BOI_QUEUE_DIR      — spec queue directory (default: ~/.boi/queue)
#   CAPTURE_DIR        — where to write assessments (default: $AGENT_DIR/raw/captures)
#   ASSESS_OUTPUT      — "capture" (default) or "stdout" (for piping)
#
# Outputs a next-action assessment as markdown. By default writes to
# $CAPTURE_DIR for the agent to pick up. Set ASSESS_OUTPUT=stdout to
# print to stdout instead (useful for scripting or testing).

set -uo pipefail

SPEC_ID="${1:-}"
SPEC_TITLE="${2:-}"
TARGET_REPO="${3:-}"

# Configurable paths — no hardcoded user dirs
BOI_QUEUE_DIR="${BOI_QUEUE_DIR:-$HOME/.boi/queue}"
AGENT_DIR="${AGENT_DIR:-}"
CAPTURE_DIR="${CAPTURE_DIR:-${AGENT_DIR:+$AGENT_DIR/raw/captures}}"
ASSESS_OUTPUT="${ASSESS_OUTPUT:-capture}"

TIMESTAMP="$(date '+%Y-%m-%d %H:%M')"

if [[ -z "$SPEC_ID" ]]; then
    echo "Usage: $0 <spec_id> [spec_title] [target_repo]" >&2
    exit 1
fi

# ── Gather signals ─────────────────────────────────────────────────────

SPEC_FILE="${BOI_QUEUE_DIR}/${SPEC_ID}.spec.md"
NEXT_PHASE=""
WAITING_SPECS=""
SAME_PROJECT_PENDING=""

# 1. Check if spec references next phases or dependencies
if [[ -f "$SPEC_FILE" ]]; then
    NEXT_PHASE=$(grep -i "next.*phase\|phase.*[0-9]\|depends.*on.*this\|blocked.*on.*${SPEC_ID}" "$SPEC_FILE" 2>/dev/null | head -3)
fi

# 2. Check if any queued specs depend on this one
if [[ -d "$BOI_QUEUE_DIR" ]]; then
    WAITING_SPECS=$(find "$BOI_QUEUE_DIR" -name "*.spec.md" -exec grep -l "depends.*${SPEC_ID}\|blocked.*${SPEC_ID}\|after.*${SPEC_ID}" {} \; 2>/dev/null | head -5)
fi

# 3. Check for other pending specs targeting the same repo
if [[ -n "$TARGET_REPO" && -d "$BOI_QUEUE_DIR" && -f "$SPEC_FILE" ]]; then
    SAME_PROJECT_PENDING=$(find "$BOI_QUEUE_DIR" -name "*.spec.md" -newer "$SPEC_FILE" -exec grep -l "$TARGET_REPO" {} \; 2>/dev/null | head -5)
fi

# ── Build assessment ───────────────────────────────────────────────────

build_assessment() {
    cat << EOF
# Spec Completed: ${SPEC_ID} — Next Action Assessment

**Spec:** ${SPEC_TITLE:-$SPEC_ID}
**Completed:** ${TIMESTAMP}
${TARGET_REPO:+**Target:** $TARGET_REPO}

## Status
${SPEC_ID} completed. Assess what happens next.

## Signals
EOF

    local has_signals=false

    if [[ -n "$WAITING_SPECS" ]]; then
        has_signals=true
        echo "### Specs waiting on this:"
        echo "$WAITING_SPECS" | while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            echo "- $(basename "$f" .spec.md): $(head -1 "$f" | sed 's/^# //')"
        done
        echo ""
    fi

    if [[ -n "$NEXT_PHASE" ]]; then
        has_signals=true
        echo "### Next phase references in spec:"
        echo "$NEXT_PHASE"
        echo ""
    fi

    if [[ -n "$SAME_PROJECT_PENDING" ]]; then
        has_signals=true
        echo "### Other pending specs for same project:"
        echo "$SAME_PROJECT_PENDING" | while IFS= read -r f; do
            [[ -z "$f" ]] && continue
            echo "- $(basename "$f" .spec.md)"
        done
        echo ""
    fi

    if [[ "$has_signals" == "false" ]]; then
        echo "No dependent specs, next phases, or related pending work found."
        echo ""
    fi

    echo "## Recommended Action"
    echo "Review ${SPEC_ID} output. If this was part of a phased build, write and dispatch the next phase informed by what was built."
}

# ── Output ─────────────────────────────────────────────────────────────

if [[ "$ASSESS_OUTPUT" == "stdout" ]]; then
    build_assessment
else
    if [[ -z "$CAPTURE_DIR" ]]; then
        echo "[assess-next-action] WARN: No AGENT_DIR or CAPTURE_DIR set. Falling back to stdout." >&2
        build_assessment
        exit 0
    fi
    CAPTURE_FILE="${CAPTURE_DIR}/next-action-${SPEC_ID}-$(date +%s).md"
    mkdir -p "$CAPTURE_DIR"
    build_assessment > "$CAPTURE_FILE"
    echo "[assess-next-action] Capture written: $CAPTURE_FILE"
fi
