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
