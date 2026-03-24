"""Tests for oneshot policy lifecycle support (t-1 RED phase).

Verifies:
- lifecycle: oneshot-delete  → YAML file deleted after firing
- lifecycle: oneshot-disable → enabled: false written to YAML after firing
- Oneshot does not fire twice
- Action failure prevents cleanup
- Default (persistent) lifecycle fires repeatedly (regression)
- max_fires: N  → auto-disables after N fires
- Validator accepts valid lifecycle values
- Validator rejects invalid lifecycle values
"""
import json
import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import EventsDB
from policy import load_policies
from hex_eventd import _process_event_policies
from policy_validator import validate_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_dir: str) -> EventsDB:
    db_path = os.path.join(tmp_dir, "test.db")
    return EventsDB(db_path)


def _make_policy_dict(name: str, event: str, action_command: str = "echo hello",
                      lifecycle: str = None, max_fires: int = None,
                      retries: int = None) -> dict:
    action = {"type": "shell", "command": action_command}
    if retries is not None:
        action["retries"] = retries
    data = {
        "name": name,
        "rules": [
            {
                "name": f"{name}-rule",
                "trigger": {"event": event},
                "actions": [action],
            }
        ],
    }
    if lifecycle is not None:
        data["lifecycle"] = lifecycle
    if max_fires is not None:
        data["max_fires"] = max_fires
    return data


def _write_policy(policies_dir: str, name: str, data: dict) -> str:
    path = os.path.join(policies_dir, f"{name}.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _emit(db: EventsDB, event_type: str, payload: dict = None) -> int:
    return db.insert_event(event_type, json.dumps(payload or {}), "test")


def _tick(db: EventsDB, policies: list) -> int:
    events = db.get_unprocessed()
    for event in events:
        _process_event_policies(event, policies, db)
    return len(events)


def _make_counter_script(tmp_dir: str) -> tuple[str, str]:
    """Returns (script_path, count_file_path). Script appends 'x' to count_file."""
    count_file = os.path.join(tmp_dir, "count.txt")
    script = os.path.join(tmp_dir, "counter.sh")
    with open(script, "w") as f:
        f.write(f"#!/bin/sh\necho x >> {count_file}\n")
    os.chmod(script, 0o755)
    return script, count_file


def _read_count(count_file: str) -> int:
    if not os.path.exists(count_file):
        return 0
    with open(count_file) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    return len(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_oneshot_delete_removes_file():
    """Policy with lifecycle: oneshot-delete fires once, then YAML file is deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        data = _make_policy_dict("one-and-done", "test.ping", lifecycle="oneshot-delete")
        policy_path = _write_policy(policies_dir, "one-and-done", data)
        assert os.path.exists(policy_path)

        policies = load_policies(policies_dir)
        assert len(policies) == 1

        _emit(db, "test.ping")
        _tick(db, policies)

        assert not os.path.exists(policy_path), (
            "oneshot-delete policy YAML should be deleted after firing"
        )


def test_oneshot_disable_sets_enabled_false():
    """Policy with lifecycle: oneshot-disable fires once, then enabled: false is written."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        data = _make_policy_dict("disable-me", "test.ping", lifecycle="oneshot-disable")
        policy_path = _write_policy(policies_dir, "disable-me", data)

        policies = load_policies(policies_dir)
        assert len(policies) == 1

        _emit(db, "test.ping")
        _tick(db, policies)

        assert os.path.exists(policy_path), (
            "oneshot-disable policy YAML should still exist after firing"
        )
        with open(policy_path) as f:
            updated = yaml.safe_load(f)
        assert updated.get("enabled") is False, (
            "oneshot-disable policy should have enabled: false after firing"
        )


def test_oneshot_does_not_fire_twice():
    """After oneshot-delete policy fires, reloading shows no policy → fires exactly once."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        script, count_file = _make_counter_script(tmp)
        data = _make_policy_dict("oneshot-once", "test.event",
                                 action_command=f"bash {script}",
                                 lifecycle="oneshot-delete")
        _write_policy(policies_dir, "oneshot-once", data)

        # First tick: fires (script runs), then deletes the YAML
        policies = load_policies(policies_dir)
        _emit(db, "test.event")
        _tick(db, policies)

        # Reload policies (file should be gone) — no policies remain
        policies = load_policies(policies_dir)

        # Second tick: no policies, event does nothing
        _emit(db, "test.event")
        _tick(db, policies)

        fires = _read_count(count_file)
        assert fires == 1, f"Policy should fire exactly once, fired {fires} times"


def test_oneshot_not_removed_on_action_failure():
    """If the policy action fails, the YAML file is NOT deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        # retries=0 so the test doesn't sleep during backoff
        data = _make_policy_dict("fail-policy", "test.ping",
                                 action_command="exit 1",
                                 lifecycle="oneshot-delete",
                                 retries=0)
        policy_path = _write_policy(policies_dir, "fail-policy", data)

        policies = load_policies(policies_dir)
        _emit(db, "test.ping")
        _tick(db, policies)

        assert os.path.exists(policy_path), (
            "Policy file should NOT be deleted when action fails"
        )


def test_persistent_lifecycle_fires_repeatedly():
    """Default lifecycle (persistent) fires on every matching event — regression check."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        script, count_file = _make_counter_script(tmp)
        # No lifecycle field → persistent (default)
        data = _make_policy_dict("persistent-policy", "test.event",
                                 action_command=f"bash {script}")
        _write_policy(policies_dir, "persistent-policy", data)

        policies = load_policies(policies_dir)
        for _ in range(3):
            _emit(db, "test.event")
        _tick(db, policies)

        fires = _read_count(count_file)
        assert fires == 3, f"Persistent policy should fire 3 times, fired {fires}"


def test_max_fires_limits_execution():
    """Policy with max_fires: 2 auto-disables after firing twice."""
    with tempfile.TemporaryDirectory() as tmp:
        policies_dir = os.path.join(tmp, "policies")
        os.makedirs(policies_dir)
        db = _make_db(tmp)

        script, count_file = _make_counter_script(tmp)
        data = _make_policy_dict("max-fires-policy", "test.event",
                                 action_command=f"bash {script}",
                                 max_fires=2)
        policy_path = _write_policy(policies_dir, "max-fires-policy", data)

        # Fire 3 separate ticks; reload policies between each to pick up file changes
        for _ in range(3):
            policies = load_policies(policies_dir)
            _emit(db, "test.event")
            _tick(db, policies)

        fires = _read_count(count_file)
        assert fires == 2, f"max_fires=2 policy should fire exactly 2 times, fired {fires}"

        # Policy file should be auto-disabled
        with open(policy_path) as f:
            updated = yaml.safe_load(f)
        assert updated.get("enabled") is False, (
            "Policy with max_fires exhausted should be auto-disabled"
        )


def test_validator_accepts_lifecycle_field():
    """Policy with lifecycle: oneshot-delete passes validation."""
    policy = {
        "name": "test-oneshot",
        "lifecycle": "oneshot-delete",
        "rules": [
            {
                "name": "rule-1",
                "trigger": {"event": "test.event"},
                "actions": [{"type": "shell", "command": "echo hi"}],
            }
        ],
    }
    errors = validate_policy(policy, "test.yaml")
    assert errors == [], f"Expected no validation errors, got: {errors}"


def test_validator_rejects_invalid_lifecycle():
    """Policy with lifecycle: bogus fails validation with a clear error."""
    policy = {
        "name": "test-bad-lifecycle",
        "lifecycle": "bogus",
        "rules": [
            {
                "name": "rule-1",
                "trigger": {"event": "test.event"},
                "actions": [{"type": "shell", "command": "echo hi"}],
            }
        ],
    }
    errors = validate_policy(policy, "test.yaml")
    assert len(errors) >= 1, "Expected validation errors for invalid lifecycle value"
    assert any("lifecycle" in e.lower() for e in errors), (
        f"Error should mention 'lifecycle', got: {errors}"
    )
