#!/usr/bin/env bash
# verify-boi-completion.sh — Verify a BOI spec target repo is clean, tested, pushed.
# Usage: bash verify-boi-completion.sh <spec_id> <target_repo_path>
# Exit 0 = verified clean. Exit 1 = verification failed.

set -uo pipefail

SPEC_ID="${1:-}"
TARGET_REPO="${2:-}"

if [[ -z "$SPEC_ID" || -z "$TARGET_REPO" ]]; then
    echo "Usage: $0 <spec_id> <target_repo_path>" >&2
    exit 1
fi

# Expand ~ if present
TARGET_REPO="${TARGET_REPO/#\~/$HOME}"

# ── 1. cd into target repo ───────────────────────────────────────────────────
echo "[CHECK] Entering repo: $TARGET_REPO"
if [[ ! -d "$TARGET_REPO" ]]; then
    echo "[FAIL]  Repo does not exist: $TARGET_REPO" >&2
    exit 1
fi
cd "$TARGET_REPO"

# ── 2. Uncommitted changes ────────────────────────────────────────────────────
echo "[CHECK] git status (uncommitted changes)"
DIRTY=$(git status --porcelain 2>/dev/null)
if [[ -n "$DIRTY" ]]; then
    echo "[FAIL]  Uncommitted changes in $TARGET_REPO:" >&2
    echo "$DIRTY" >&2
    exit 1
fi
echo "[OK]    Working tree is clean"

# ── 3. Unpushed commits ───────────────────────────────────────────────────────
echo "[CHECK] git diff origin/HEAD..HEAD (unpushed commits)"
# Fetch quietly so we compare against latest remote state
git fetch --quiet 2>/dev/null || true
UNPUSHED=$(git diff origin/HEAD..HEAD 2>/dev/null)
if [[ -n "$UNPUSHED" ]]; then
    echo "[FAIL]  Unpushed commits in $TARGET_REPO" >&2
    git log origin/HEAD..HEAD --oneline 2>/dev/null >&2
    exit 1
fi
echo "[OK]    No unpushed commits"

# ── 4. Tests ──────────────────────────────────────────────────────────────────
if [[ -f "Makefile" ]] && grep -q "^test" Makefile 2>/dev/null; then
    echo "[CHECK] Running: make test"
    if ! make test; then
        echo "[FAIL]  make test failed in $TARGET_REPO" >&2
        exit 1
    fi
    echo "[OK]    make test passed"
elif [[ -f "pytest.ini" || -d "tests" ]]; then
    echo "[CHECK] Running: python3 -m pytest tests/ --tb=short -q"
    if ! python3 -m pytest tests/ --tb=short -q; then
        echo "[FAIL]  pytest failed in $TARGET_REPO" >&2
        exit 1
    fi
    echo "[OK]    pytest passed"
else
    echo "[SKIP]  No test runner found (no Makefile test target, pytest.ini, or tests/)"
fi

# ── 5. All clear ─────────────────────────────────────────────────────────────
echo "VERIFIED: $SPEC_ID clean, tested, pushed"
exit 0
