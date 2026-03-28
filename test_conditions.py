# test_conditions.py
import json
import os
import tempfile
import pytest
from db import EventsDB
from conditions import evaluate_conditions
from recipe import Condition

@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = EventsDB(path)
    yield d
    d.close()
    os.unlink(path)

def test_simple_eq():
    payload = {"status": "completed"}
    conds = [Condition(field="status", op="eq", value="completed")]
    assert evaluate_conditions(conds, payload, db=None) is True

def test_simple_neq():
    payload = {"status": "failed"}
    conds = [Condition(field="status", op="eq", value="completed")]
    assert evaluate_conditions(conds, payload, db=None) is False

def test_gt():
    payload = {"count": 5}
    conds = [Condition(field="count", op="gt", value=3)]
    assert evaluate_conditions(conds, payload, db=None) is True

def test_gte():
    payload = {"count": 3}
    conds = [Condition(field="count", op="gte", value=3)]
    assert evaluate_conditions(conds, payload, db=None) is True

def test_contains():
    payload = {"message": "BOI q-095 completed successfully"}
    conds = [Condition(field="message", op="contains", value="completed")]
    assert evaluate_conditions(conds, payload, db=None) is True

def test_multiple_conditions_all_must_match():
    payload = {"status": "completed", "spec_id": "q-095"}
    conds = [
        Condition(field="status", op="eq", value="completed"),
        Condition(field="spec_id", op="eq", value="q-095"),
    ]
    assert evaluate_conditions(conds, payload, db=None) is True

def test_multiple_conditions_one_fails():
    payload = {"status": "completed", "spec_id": "q-096"}
    conds = [
        Condition(field="status", op="eq", value="completed"),
        Condition(field="spec_id", op="eq", value="q-095"),
    ]
    assert evaluate_conditions(conds, payload, db=None) is False

def test_count_condition(db):
    for _ in range(3):
        db.insert_event("boi.spec.failed", '{}', "boi-daemon")
    conds = [Condition(field="count(boi.spec.failed, 1)", op="gte", value=3)]
    assert evaluate_conditions(conds, {}, db=db) is True

def test_count_condition_not_met(db):
    db.insert_event("boi.spec.failed", '{}', "boi-daemon")
    conds = [Condition(field="count(boi.spec.failed, 1)", op="gte", value=3)]
    assert evaluate_conditions(conds, {}, db=db) is False

def test_no_conditions_always_true():
    assert evaluate_conditions([], {}, db=None) is True


# ---------------------------------------------------------------------------
# Policy loading tests — verify condition loading from YAML
# ---------------------------------------------------------------------------

import textwrap
from policy import _parse_rule, load_policies


def test_trigger_conditions_loaded():
    """Conditions nested under trigger: are parsed into Rule.conditions."""
    rule_data = {
        "name": "test-rule",
        "trigger": {
            "event": "boi.spec.completed",
            "conditions": [
                {"field": "spec_id", "op": "eq", "value": "q-310"}
            ],
        },
        "actions": [],
    }
    rule = _parse_rule(rule_data, "test-policy", 0)
    assert len(rule.conditions) == 1
    c = rule.conditions[0]
    assert c.field == "spec_id"
    assert c.op == "eq"
    assert c.value == "q-310"


def test_rule_level_conditions_loaded():
    """Conditions at the rule level (not under trigger:) are loaded."""
    rule_data = {
        "name": "test-rule",
        "trigger": {"event": "boi.spec.completed"},
        "conditions": [
            {"field": "status", "op": "eq", "value": "done"}
        ],
        "actions": [],
    }
    rule = _parse_rule(rule_data, "test-policy", 0)
    assert len(rule.conditions) == 1
    assert rule.conditions[0].field == "status"
    assert rule.conditions[0].op == "eq"
    assert rule.conditions[0].value == "done"


def test_policy_conditions_filter_events():
    """A rule with conditions fires only when the event payload matches."""
    rule_data = {
        "name": "filter-rule",
        "trigger": {
            "event": "boi.spec.completed",
            "conditions": [
                {"field": "spec_id", "op": "eq", "value": "q-310"}
            ],
        },
        "actions": [],
    }
    rule = _parse_rule(rule_data, "test-policy", 0)
    assert evaluate_conditions(rule.conditions, {"spec_id": "q-310"}, db=None) is True
    assert evaluate_conditions(rule.conditions, {"spec_id": "q-999"}, db=None) is False


def test_load_policies_trigger_conditions(tmp_path):
    """load_policies correctly reads trigger.conditions into Rule.conditions."""
    yaml_content = textwrap.dedent("""\
        name: test-cond-policy
        rules:
          - name: my-rule
            trigger:
              event: boi.spec.completed
              conditions:
                - field: spec_id
                  op: eq
                  value: q-310
            actions:
              - type: emit
                event: test.matched
    """)
    policy_file = tmp_path / "test_cond.yaml"
    policy_file.write_text(yaml_content)
    policies = load_policies(str(tmp_path))
    assert len(policies) == 1
    rule = policies[0].rules[0]
    assert len(rule.conditions) == 1
    c = rule.conditions[0]
    assert c.field == "spec_id"
    assert c.op == "eq"
    assert c.value == "q-310"
