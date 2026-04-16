#!/usr/bin/env bash
# hex-events Docker E2E test
# Tests install, core files, dependencies, no personal refs, policy parsing,
# event emission, condition evaluation, unit tests, README, and LICENSE.
# Exit 0 = all checks passed; non-zero = failure.
set -uo pipefail

SETUP_DIR="/tmp/hex-events-setup"
PASS=0
FAIL=0
FAILURES=()

pass() { echo "[PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); FAILURES+=("$1"); }

# ---------------------------------------------------------------------------
# 1. Install
# ---------------------------------------------------------------------------
echo "==> 1. Install"
INSTALL_OUT=$(HEX_EVENTS_NO_LAUNCHCTL=1 bash "$SETUP_DIR/install.sh" 2>&1)
INSTALL_EXIT=$?
if [ "$INSTALL_EXIT" -eq 0 ]; then
    pass "install.sh completed successfully"
else
    fail "install.sh exited with $INSTALL_EXIT"
    echo "$INSTALL_OUT" | tail -20
fi

# ---------------------------------------------------------------------------
# 2. Core files exist
# ---------------------------------------------------------------------------
echo ""
echo "==> 2. Core files exist"
for f in hex_eventd.py db.py policy.py conditions.py hex_emit.py hex_events_cli.py; do
    if [ -f "$SETUP_DIR/$f" ]; then
        pass "core file exists: $f"
    else
        fail "core file missing: $f"
    fi
done

# ---------------------------------------------------------------------------
# 3. Dependencies install cleanly
# ---------------------------------------------------------------------------
echo ""
echo "==> 3. Dependencies"
if cd "$SETUP_DIR" && pip install -q -r requirements.txt 2>&1; then
    pass "pip install -r requirements.txt succeeded"
else
    fail "pip install -r requirements.txt failed"
fi

# ---------------------------------------------------------------------------
# 4. No personal references in Python source files
# ---------------------------------------------------------------------------
echo ""
echo "==> 4. No personal references in Python source"
PERSONAL_REFS=0
# Only scan top-level Python source files, not scripts/, tests/, or venv/
for pattern in "com\.mrap" "rapadas" '"mike"'; do
    MATCHES=$(grep -n "$pattern" \
        "$SETUP_DIR/hex_eventd.py" \
        "$SETUP_DIR/db.py" \
        "$SETUP_DIR/policy.py" \
        "$SETUP_DIR/conditions.py" \
        "$SETUP_DIR/hex_emit.py" \
        "$SETUP_DIR/hex_events_cli.py" \
        "$SETUP_DIR/hex_healthcheck.py" \
        "$SETUP_DIR/policy_validator.py" \
        "$SETUP_DIR/validator.py" \
        "$SETUP_DIR/recipe.py" \
        2>/dev/null | wc -l | tr -d ' ')
    if [ "$MATCHES" -gt 0 ]; then
        fail "personal ref '$pattern' found in core Python source ($MATCHES matches)"
        grep -n "$pattern" \
            "$SETUP_DIR/hex_eventd.py" \
            "$SETUP_DIR/db.py" \
            "$SETUP_DIR/policy.py" \
            "$SETUP_DIR/conditions.py" \
            "$SETUP_DIR/hex_emit.py" \
            "$SETUP_DIR/hex_events_cli.py" \
            "$SETUP_DIR/hex_healthcheck.py" \
            "$SETUP_DIR/policy_validator.py" \
            "$SETUP_DIR/validator.py" \
            "$SETUP_DIR/recipe.py" \
            2>/dev/null | head -5
        PERSONAL_REFS=$((PERSONAL_REFS+1))
    fi
done
if [ "$PERSONAL_REFS" -eq 0 ]; then
    pass "no personal references found in core Python source files"
fi

# ---------------------------------------------------------------------------
# 5. Policy validation: .yaml files exist and parse
# ---------------------------------------------------------------------------
echo ""
echo "==> 5. Policy validation"
POLICIES_DIR="$SETUP_DIR/policies"
YAML_COUNT=$(find "$POLICIES_DIR" -name "*.yaml" -not -path "*/disabled/*" 2>/dev/null | wc -l | tr -d ' ')
if [ "$YAML_COUNT" -gt 0 ]; then
    pass "policies directory has $YAML_COUNT .yaml files"
else
    fail "no .yaml files found in policies/"
fi

PARSE_FAIL=0
while IFS= read -r -d '' yaml_file; do
    if cd "$SETUP_DIR" && python3 -c "import yaml; yaml.safe_load(open('$yaml_file'))" 2>/dev/null; then
        :
    else
        fail "policy yaml parse failed: $yaml_file"
        PARSE_FAIL=$((PARSE_FAIL+1))
    fi
done < <(find "$POLICIES_DIR" -name "*.yaml" -not -path "*/disabled/*" -print0 2>/dev/null)
if [ "$PARSE_FAIL" -eq 0 ] && [ "$YAML_COUNT" -gt 0 ]; then
    pass "all $YAML_COUNT policy yaml files parse cleanly"
fi

# ---------------------------------------------------------------------------
# 6. Event emission
# ---------------------------------------------------------------------------
echo ""
echo "==> 6. Event emission"
cd "$SETUP_DIR"
EVENT_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$SETUP_DIR')
from db import EventsDB
db = EventsDB('/tmp/test-e2e-events.db')
db.insert_event('test.event', '{\"key\": \"value\"}', 'e2e-test')
events = db.get_unprocessed()
assert len(events) == 1, f'Expected 1 event, got {len(events)}'
assert events[0]['event_type'] == 'test.event', f'Wrong event type: {events[0][\"event_type\"]}'
print('Event emission works')
" 2>&1)
if echo "$EVENT_RESULT" | grep -q "Event emission works"; then
    pass "event emission: EventsDB.insert_event and get_unprocessed work"
else
    fail "event emission failed"
    echo "    Output: $EVENT_RESULT"
fi

# ---------------------------------------------------------------------------
# 7. Condition evaluation
# ---------------------------------------------------------------------------
echo ""
echo "==> 7. Condition evaluation"
cd "$SETUP_DIR"
COND_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$SETUP_DIR')
from conditions import evaluate_conditions
from policy import Condition
conds = [Condition(field='status', op='eq', value='done')]
result = evaluate_conditions(conds, {'status': 'done'}, db=None)
assert result == True, f'Expected True, got {result}'
print('Condition evaluation works')
" 2>&1)
if echo "$COND_RESULT" | grep -q "Condition evaluation works"; then
    pass "condition evaluation: evaluate_conditions works"
else
    fail "condition evaluation failed"
    echo "    Output: $COND_RESULT"
fi

# ---------------------------------------------------------------------------
# 8. Unit tests pass (excluding test_stress.py)
# ---------------------------------------------------------------------------
echo ""
echo "==> 8. Unit tests"
cd "$SETUP_DIR"
PYTEST_OUT=$(python3 -m pytest tests/ -q --tb=short --ignore=tests/test_stress.py 2>&1)
PYTEST_EXIT=$?
if [ "$PYTEST_EXIT" -eq 0 ]; then
    pass "unit test suite passed"
else
    fail "unit test suite failed (exit $PYTEST_EXIT)"
    echo "$PYTEST_OUT" | tail -30
fi

# ---------------------------------------------------------------------------
# 9. README exists
# ---------------------------------------------------------------------------
echo ""
echo "==> 9. README"
if [ -f "$SETUP_DIR/README.md" ]; then
    pass "README.md exists"
else
    fail "README.md not found"
fi

# ---------------------------------------------------------------------------
# 10. LICENSE exists
# ---------------------------------------------------------------------------
echo ""
echo "==> 10. LICENSE"
if [ -f "$SETUP_DIR/LICENSE" ]; then
    pass "LICENSE exists"
else
    fail "LICENSE not found"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo " E2E TEST RESULTS"
echo "========================================"
echo " Passed: $PASS"
echo " Failed: $FAIL"
if [ ${#FAILURES[@]} -gt 0 ]; then
    echo ""
    echo " Failures:"
    for f in "${FAILURES[@]}"; do
        echo "   - $f"
    done
fi
echo "========================================"

if [ "$FAIL" -eq 0 ]; then
    echo " PASS — all E2E checks passed"
    exit 0
else
    echo " FAIL — $FAIL check(s) failed"
    exit 1
fi
