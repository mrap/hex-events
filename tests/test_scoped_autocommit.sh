#!/usr/bin/env bash
# tests/test_scoped_autocommit.sh — Integration test for scoped auto-commit with manifest
# Tests that auto-commit-boi-output.sh only stages files listed in the manifest
# Usage: bash tests/test_scoped_autocommit.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTO_COMMIT_SCRIPT="$REPO_ROOT/scripts/auto-commit-boi-output.sh"

PASS=0
FAIL=0
TEMP_REPO=""
MANIFEST=""

_cleanup() {
    [[ -n "$TEMP_REPO" && -d "$TEMP_REPO" ]] && rm -rf "$TEMP_REPO"
    [[ -n "$MANIFEST" && -f "$MANIFEST" ]] && rm -f "$MANIFEST"
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

# ---------------------------------------------------------------------------
# Setup: create a temp git repo with a known initial commit
# ---------------------------------------------------------------------------
echo "=== Setup ==="
TEMP_REPO="$(mktemp -d)"
git -C "$TEMP_REPO" init -q
git -C "$TEMP_REPO" -c user.email="test@test.com" -c user.name="Test" \
    commit --allow-empty -m "init" -q

# Create two dirty files: one the "spec" changed, one a concurrent change
echo "spec output content" > "$TEMP_REPO/spec-output.txt"
echo "concurrent change content" > "$TEMP_REPO/concurrent-change.txt"

MANIFEST="$(mktemp)"
echo "spec-output.txt" > "$MANIFEST"

echo "  Temp repo: $TEMP_REPO"
echo "  Manifest:  $MANIFEST"

# ---------------------------------------------------------------------------
# Test 1: Run auto-commit WITH manifest — only spec-output.txt should be staged
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 1: Scoped commit with manifest ==="

EXIT_CODE=0
bash "$AUTO_COMMIT_SCRIPT" "test-q-999" "$TEMP_REPO" "$MANIFEST" \
    >/dev/null 2>/dev/null || EXIT_CODE=$?

_assert "exit code 0" "$EXIT_CODE"

HEAD_FILES="$(git -C "$TEMP_REPO" show HEAD --name-only --format='' 2>/dev/null)"
_assert_contains "HEAD includes spec-output.txt" "$HEAD_FILES" "spec-output.txt"
_assert_not_contains "HEAD does NOT include concurrent-change.txt" "$HEAD_FILES" "concurrent-change.txt"

DIRTY="$(git -C "$TEMP_REPO" status --porcelain 2>/dev/null)"
_assert_contains "concurrent-change.txt still dirty after scoped commit" "$DIRTY" "concurrent-change.txt"
_assert_not_contains "spec-output.txt no longer dirty" "$DIRTY" "spec-output.txt"

# ---------------------------------------------------------------------------
# Test 2: Fallback — no manifest, should stage all dirty files with warning
# ---------------------------------------------------------------------------
echo ""
echo "=== Test 2: Fallback (no manifest) ==="

# Add another dirty file
echo "fallback test content" > "$TEMP_REPO/fallback-test.txt"

FALLBACK_OUTPUT="$(bash "$AUTO_COMMIT_SCRIPT" "test-q-998" "$TEMP_REPO" 2>&1)" || true

_assert_contains "warning mentions falling back to git add -A" \
    "$FALLBACK_OUTPUT" "falling back to git add -A"

HEAD_FILES_2="$(git -C "$TEMP_REPO" show HEAD --name-only --format='' 2>/dev/null)"
_assert_contains "fallback HEAD includes concurrent-change.txt" \
    "$HEAD_FILES_2" "concurrent-change.txt"
_assert_contains "fallback HEAD includes fallback-test.txt" \
    "$HEAD_FILES_2" "fallback-test.txt"

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
