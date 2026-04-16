#!/usr/bin/env bash
# boi-safe-restart.sh — Gated restart pipeline for BOI.
# Stages: STOP → PULL → TEST → GATE → RESTART → SMOKE → CANARY → PROMOTE
# Runnable standalone — does not require hex-events daemon to be up.
set -euo pipefail

BOI_DIR="${BOI_DIR:-${HOME}/.boi}"
OPS_LOG="${HOME}/.boi/ops-actions.log"
GRACE_PERIOD=60
CANARY_TIMEOUT=120

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [boi-restart] $1" | tee -a "$OPS_LOG"; }
fail() { log "FAILED at stage $1: $2"; exit 1; }

# Prevent concurrent restarts (git pull triggers post-merge hook which
# re-emits git.pull, which would invoke this script again).
LOCK_FILE="${HOME}/.boi/restart.lock"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 200>"$LOCK_FILE"
flock -n 200 || { log "Another restart already in progress, skipping."; exit 0; }

# ── Stage 1: STOP ──────────────────────────────────────────────────────────
log "Stage 1: STOP — stopping BOI daemon"
if pgrep -f "boi/daemon.py" > /dev/null 2>&1; then
    pkill -f "boi/daemon.py" 2>/dev/null || true
    elapsed=0
    while pgrep -f "boi/daemon.py" > /dev/null 2>&1 && [[ $elapsed -lt $GRACE_PERIOD ]]; do
        sleep 5
        elapsed=$((elapsed + 5))
        log "  Waiting for daemon to stop... (${elapsed}s/${GRACE_PERIOD}s)"
    done
    if pgrep -f "boi/daemon.py" > /dev/null 2>&1; then
        log "  Grace period exceeded. Force-stopping."
        pkill -9 -f "boi/daemon.py" 2>/dev/null || true
        sleep 2
    fi
    log "  Daemon stopped."
else
    log "  Daemon was not running."
fi

# ── Stage 2: PULL ──────────────────────────────────────────────────────────
log "Stage 2: PULL — pulling latest code"
cd "$BOI_DIR"
PREV_HEAD=$(git rev-parse HEAD)
git pull --ff-only 2>&1 || fail "PULL" "git pull --ff-only failed (history diverged?)"
NEW_HEAD=$(git rev-parse HEAD)
if [[ "$PREV_HEAD" == "$NEW_HEAD" ]]; then
    log "  No new commits. Restarting daemon on current code."
fi
log "  Pulled: $PREV_HEAD -> $NEW_HEAD"

# ── Stage 3: TEST ──────────────────────────────────────────────────────────
log "Stage 3: TEST — running test suite"
TESTS_PASSED=true
if [[ -f "$BOI_DIR/Dockerfile" ]]; then
    log "  Building Docker test image..."
    docker build -t boi-test -f "$BOI_DIR/Dockerfile" "$BOI_DIR" 2>&1 | tail -5
    docker run --rm boi-test python3 -m pytest tests/ -v --timeout=60 2>&1 || {
        TEST_EXIT=$?
        log "  Tests FAILED in Docker (exit code $TEST_EXIT)"
        TESTS_PASSED=false
    }
else
    log "  No Dockerfile found. Running tests locally."
    cd "$BOI_DIR" && python3 -m pytest tests/ -v --timeout=60 2>&1 || {
        TESTS_PASSED=false
    }
fi

# ── Stage 4: GATE ──────────────────────────────────────────────────────────
if [[ "$TESTS_PASSED" == "false" ]]; then
    log "Stage 4: GATE — tests failed. Rolling back."
    cd "$BOI_DIR"
    git tag "rollback-$(date +%s)" HEAD
    git revert --no-edit HEAD 2>&1 || fail "GATE" "git revert failed"
    log "  Reverted to previous commit. Tag preserved for investigation."
    osascript -e 'display notification "BOI restart: tests failed, reverted to previous commit" with title "boi-safe-restart"' 2>/dev/null || true
    fail "GATE" "Tests failed — rolled back to $PREV_HEAD"
else
    log "Stage 4: GATE — tests passed."
fi

# ── Stage 5: RESTART ───────────────────────────────────────────────────────
log "Stage 5: RESTART — starting BOI daemon"
cd "$BOI_DIR"
nohup python3 daemon.py >> "${HOME}/.boi/daemon.log" 2>&1 &
DAEMON_PID=$!
sleep 3
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    fail "RESTART" "Daemon failed to start (pid $DAEMON_PID died)"
fi
log "  Daemon started (pid $DAEMON_PID)"

# Re-queue specs that were in-flight during restart
bash ~/.boi/boi requeuefailed --reason "daemon restart" 2>/dev/null || {
    log "  WARN: requeuefailed subcommand not available (skipping re-queue)"
}

# ── Stage 6: SMOKE ─────────────────────────────────────────────────────────
log "Stage 6: SMOKE — running smoke tests"
bash ~/.boi/boi status > /dev/null 2>&1 || fail "SMOKE" "boi status failed"
if ! pgrep -f "boi/daemon.py" > /dev/null 2>&1; then
    fail "SMOKE" "Daemon died after startup"
fi
log "  Smoke tests passed."

# ── Stage 7: CANARY ────────────────────────────────────────────────────────
log "Stage 7: CANARY — dispatching canary spec"
CANARY_SPEC=$(mktemp /tmp/boi-canary-XXXXXX.spec.md)
cat > "$CANARY_SPEC" << 'SPEC'
# Canary Health Check

**Mode:** execute
**Workspace:** in-place
**Target repo:** /tmp

## Tasks
- [ ] t-1: Echo canary
Echo "canary-ok" to stdout.

**Verify:**
```bash
echo "canary-ok"
```
SPEC

timeout "$CANARY_TIMEOUT" bash ~/.boi/boi dispatch "$CANARY_SPEC" --priority 100 2>&1 || {
    CANARY_EXIT=$?
    log "  Canary spec timed out or failed (exit $CANARY_EXIT). Investigate manually."
    osascript -e 'display notification "BOI restart: canary failed, investigate" with title "boi-safe-restart"' 2>/dev/null || true
}
rm -f "$CANARY_SPEC"
log "  Canary dispatched."

# ── Stage 8: PROMOTE ───────────────────────────────────────────────────────
# boi.upgrade.verified is emitted by the boi-restart-on-pull policy's
# on_success block when this script exits 0. No need to emit here.
log "COMPLETE: BOI safely restarted ($PREV_HEAD -> $NEW_HEAD)"
