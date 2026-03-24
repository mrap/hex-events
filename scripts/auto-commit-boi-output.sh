#!/usr/bin/env bash
# auto-commit-boi-output.sh — Auto-commit and push BOI spec output in a target repo
# Usage: auto-commit-boi-output.sh <spec_id> <target_repo_path> [<changed-files-path>]
# Part of hex-ops auto-remediation

set -uo pipefail

SPEC_ID="${1:-}"
TARGET_REPO="${2:-}"
CHANGED_FILES_MANIFEST="${3:-}"
OPS_LOG="${HOME}/.boi/ops-actions.log"

if [[ -z "$SPEC_ID" || -z "$TARGET_REPO" ]]; then
    echo "Usage: $0 <spec_id> <target_repo_path> [<changed-files-path>]" >&2
    exit 1
fi

# Resolve to absolute path
TARGET_REPO="$(cd "$TARGET_REPO" 2>/dev/null && pwd || echo "$TARGET_REPO")"

# If not a git repo, skip silently
if ! git -C "$TARGET_REPO" rev-parse --git-dir &>/dev/null; then
    echo "[auto-commit] $TARGET_REPO is not a git repo — skipping" >&2
    exit 0
fi

# Check if repo is dirty
STATUS="$(git -C "$TARGET_REPO" status --porcelain 2>/dev/null)"
if [[ -z "$STATUS" ]]; then
    echo "[auto-commit] $TARGET_REPO is clean — nothing to do"
    exit 0
fi

# Stage changes — use manifest if available, otherwise fall back to git add -A
if [[ -n "$CHANGED_FILES_MANIFEST" && -f "$CHANGED_FILES_MANIFEST" && -s "$CHANGED_FILES_MANIFEST" ]]; then
    # Read manifest and stage only the listed files (skip missing files)
    while IFS= read -r filepath || [[ -n "$filepath" ]]; do
        [[ -z "$filepath" ]] && continue
        if [[ -e "$TARGET_REPO/$filepath" ]]; then
            git -C "$TARGET_REPO" add -- "$filepath"
        else
            echo "[auto-commit] Skipping missing file from manifest: $filepath"
        fi
    done < "$CHANGED_FILES_MANIFEST"
else
    echo "[auto-commit] WARNING: No changed-files manifest for $SPEC_ID, falling back to git add -A" | tee -a "$OPS_LOG" >&2
    git -C "$TARGET_REPO" add -A
fi

# Commit
COMMIT_MSG="feat: BOI ${SPEC_ID} output — auto-committed by hex-ops"
if ! git -C "$TARGET_REPO" commit -m "$COMMIT_MSG"; then
    echo "[auto-commit] ERROR: git commit failed in $TARGET_REPO" >&2
    exit 1
fi

# Count files changed (handle initial commit case)
if git -C "$TARGET_REPO" rev-parse HEAD~1 &>/dev/null; then
    FILES_CHANGED="$(git -C "$TARGET_REPO" diff --stat HEAD~1 HEAD 2>/dev/null | tail -1 | grep -oE '[0-9]+ file' | grep -oE '[0-9]+' || echo '?')"
else
    # Initial commit — count committed files
    FILES_CHANGED="$(git -C "$TARGET_REPO" show --stat HEAD 2>/dev/null | grep -oE '[0-9]+ file' | grep -oE '[0-9]+' || echo '?')"
fi

# Push if remote exists
HAS_REMOTE="$(git -C "$TARGET_REPO" remote -v 2>/dev/null | head -1)"
PUSH_STATUS="no-remote"
if [[ -n "$HAS_REMOTE" ]]; then
    if git -C "$TARGET_REPO" push 2>/dev/null; then
        PUSH_STATUS="pushed"
    else
        PUSH_STATUS="push-failed"
        echo "[auto-commit] WARNING: git push failed in $TARGET_REPO — branch may be diverged" >&2
    fi
fi

# Log to ops-actions.log
TIMESTAMP="$(date '+%Y-%m-%d %H:%M')"
LOG_LINE="${TIMESTAMP} — auto-commit: ${SPEC_ID} in ${TARGET_REPO} (${FILES_CHANGED} files changed, push=${PUSH_STATUS})"
mkdir -p "$(dirname "$OPS_LOG")"
echo "$LOG_LINE" >> "$OPS_LOG"

echo "[auto-commit] Done: $LOG_LINE"
exit 0
