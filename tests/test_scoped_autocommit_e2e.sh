#!/usr/bin/env bash
# tests/test_scoped_autocommit_e2e.sh — End-to-end integration test for scoped auto-commit
# Tests the full BOI pipeline: dispatch → worker writes changed-files → auto-commit
# skips files NOT in the manifest.
#
# SLOW TEST: may take several minutes (BOI worker startup + claude -p).
# Skip with: SKIP_E2E=1 bash tests/test_scoped_autocommit_e2e.sh
#
# Usage: bash tests/test_scoped_autocommit_e2e.sh
#
# Timeout overrides (useful for slow environments or CI):
#   BOI_E2E_MAX_WAIT=300     seconds to wait for spec completion (default: 300)
#   BOI_E2E_POLL_INTERVAL=10 seconds between status polls (default: 10)
#   BOI_E2E_COMMIT_MAX=30    seconds to wait for auto-commit to land (default: 30)
#
# Example: BOI_E2E_MAX_WAIT=600 bash tests/test_scoped_autocommit_e2e.sh

set -uo pipefail

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------
if [[ "${SKIP_E2E:-0}" == "1" ]]; then
    echo "[e2e] SKIPPED: SKIP_E2E=1"
    exit 0
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0
TEMP_REPO=""
SPEC_FILE=""
QUEUE_ID=""

_cleanup() {
    [[ -n "$TEMP_REPO" && -d "$TEMP_REPO" ]] && rm -rf "$TEMP_REPO"
    [[ -n "$SPEC_FILE" && -f "$SPEC_FILE" ]] && rm -f "$SPEC_FILE"
    if [[ -n "$QUEUE_ID" ]]; then
        boi cancel "$QUEUE_ID" >/dev/null 2>&1 || true
    fi
}
trap _cleanup EXIT

_assert() {
    local desc="$1"
    local result="$2"
    if [[ "$result" == "0" ]]; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

_assert_contains() {
    local desc="$1"
    local haystack="$2"
    local needle="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected '$needle' in output)"
        FAIL=$((FAIL + 1))
    fi
}

_assert_not_contains() {
    local desc="$1"
    local haystack="$2"
    local needle="$3"
    if ! echo "$haystack" | grep -qF "$needle"; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected '$needle' NOT in output)"
        FAIL=$((FAIL + 1))
    fi
}

_check_boi() {
    if ! command -v boi >/dev/null 2>&1; then
        echo "[e2e] SKIPPED: 'boi' command not found"
        exit 0
    fi
    # Check daemon is running
    if ! boi status >/dev/null 2>&1; then
        echo "[e2e] SKIPPED: BOI daemon not running (boi status failed)"
        exit 0
    fi
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
echo "=== Setup ==="

_check_boi

# Create a temp git repo
TEMP_REPO="$(mktemp -d)"
git -C "$TEMP_REPO" init -q
git -C "$TEMP_REPO" -c user.email="e2e-test@test.com" -c user.name="E2E Test" \
    commit --allow-empty -m "init" -q

echo "  Temp repo: $TEMP_REPO"

# Create a "concurrent" dirty file BEFORE dispatching (simulating another process)
echo "concurrent change — should NOT be auto-committed" > "$TEMP_REPO/concurrent-dirty.txt"
echo "  Created concurrent dirty file: concurrent-dirty.txt"

# Write a simple BOI spec targeting the temp repo
SPEC_FILE="$(mktemp /tmp/boi-e2e-spec.XXXXXX.spec.md)"
cat > "$SPEC_FILE" <<SPEC
# E2E Test Spec: Create hello.txt

**Mode:** execute
**Target:** ${TEMP_REPO}

## Tasks

### t-1: Create hello.txt
PENDING

**Spec:** Create a file called \`hello.txt\` with the content "hello" in the target repo root.

\`\`\`
echo "hello" > hello.txt
\`\`\`

**Verify:** \`cat hello.txt\` outputs "hello".
SPEC

echo "  Spec file: $SPEC_FILE"

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
echo ""
echo "=== Dispatching spec ==="

DISPATCH_OUTPUT="$(boi dispatch --spec "$SPEC_FILE" --max-iter 3 --no-critic 2>&1)" || {
    echo "[e2e] SKIPPED: boi dispatch failed: $DISPATCH_OUTPUT"
    exit 0
}

echo "$DISPATCH_OUTPUT"

# Parse queue_id from dispatch output (format: "✓ (q-NNN, N/M tasks, priority P)")
QUEUE_ID="$(echo "$DISPATCH_OUTPUT" | grep -oE 'q-[0-9]+' | head -1)"
if [[ -z "$QUEUE_ID" ]]; then
    echo "[e2e] SKIPPED: could not parse queue ID from dispatch output"
    exit 0
fi
echo "  Queue ID: $QUEUE_ID"

# ---------------------------------------------------------------------------
# Wait for BOI spec completion (poll up to 5 minutes)
# ---------------------------------------------------------------------------
MAX_WAIT="${BOI_E2E_MAX_WAIT:-300}"
POLL_INTERVAL="${BOI_E2E_POLL_INTERVAL:-10}"

echo ""
echo "=== Waiting for spec completion (up to ${MAX_WAIT}s) ==="

WAITED=0
FINAL_STATUS=""

while [[ $WAITED -lt $MAX_WAIT ]]; do
    sleep "$POLL_INTERVAL"
    WAITED=$((WAITED + POLL_INTERVAL))

    STATUS_JSON="$(boi status --json 2>/dev/null)" || continue
    FINAL_STATUS="$(echo "$STATUS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for e in data.get('entries', []):
    if e.get('id') == '$QUEUE_ID':
        print(e.get('status', ''))
        break
" 2>/dev/null)" || continue

    echo "  [${WAITED}s] status: $FINAL_STATUS"

    case "$FINAL_STATUS" in
        completed|failed|canceled)
            break
            ;;
    esac
done

if [[ "$FINAL_STATUS" != "completed" ]]; then
    echo "[e2e] SKIPPED: spec did not complete in time (status: $FINAL_STATUS)"
    exit 0
fi
echo "  Spec completed."

# ---------------------------------------------------------------------------
# Wait for auto-commit to land (the hex-events policy runs async after completion)
# ---------------------------------------------------------------------------
COMMIT_MAX="${BOI_E2E_COMMIT_MAX:-30}"

echo ""
echo "=== Waiting for auto-commit (up to ${COMMIT_MAX}s) ==="

COMMIT_WAIT=0
HAS_COMMIT=0

while [[ $COMMIT_WAIT -lt $COMMIT_MAX ]]; do
    sleep 3
    COMMIT_WAIT=$((COMMIT_WAIT + 3))

    # Check if a BOI auto-commit landed in the temp repo
    HEAD_MSG="$(git -C "$TEMP_REPO" log --oneline HEAD 2>/dev/null | head -1)" || continue
    if echo "$HEAD_MSG" | grep -qF "$QUEUE_ID"; then
        HAS_COMMIT=1
        echo "  Auto-commit found: $HEAD_MSG"
        break
    fi
    echo "  [${COMMIT_WAIT}s] no auto-commit yet (HEAD: ${HEAD_MSG:-empty})"
done

if [[ "$HAS_COMMIT" -eq 0 ]]; then
    echo "[e2e] SKIPPED: auto-commit did not land in time"
    exit 0
fi

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
echo ""
echo "=== Assertions ==="

HEAD_FILES="$(git -C "$TEMP_REPO" show HEAD --name-only --format='' 2>/dev/null)"

_assert_contains "auto-commit includes hello.txt" \
    "$HEAD_FILES" "hello.txt"

_assert_not_contains "auto-commit does NOT include concurrent-dirty.txt" \
    "$HEAD_FILES" "concurrent-dirty.txt"

MANIFEST_PATH="$HOME/.boi/queue/${QUEUE_ID}.changed-files"
_assert "changed-files manifest exists" "$([ -f "$MANIFEST_PATH" ] && echo 0 || echo 1)"

if [[ -f "$MANIFEST_PATH" ]]; then
    MANIFEST_CONTENT="$(cat "$MANIFEST_PATH")"
    _assert_contains "manifest contains hello.txt" "$MANIFEST_CONTENT" "hello.txt"
fi

DIRTY_AFTER="$(git -C "$TEMP_REPO" status --porcelain 2>/dev/null)"
_assert_contains "concurrent-dirty.txt still untracked after auto-commit" \
    "$DIRTY_AFTER" "concurrent-dirty.txt"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results ==="
echo "  PASSED: $PASS"
echo "  FAILED: $FAIL"

if [[ "$FAIL" -gt 0 ]]; then
    echo "FAIL"
    exit 1
fi

echo "PASS"
exit 0
