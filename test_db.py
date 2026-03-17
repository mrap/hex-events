# test_db.py
import os
import tempfile
import pytest
from db import EventsDB

@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = EventsDB(path)
    yield d
    d.close()
    os.unlink(path)

def test_insert_event(db):
    eid = db.insert_event("boi.spec.completed", '{"spec_id":"q-095"}', "boi-daemon")
    assert eid == 1

def test_get_unprocessed(db):
    db.insert_event("boi.spec.completed", '{"spec_id":"q-095"}', "boi-daemon")
    db.insert_event("session.stop", '{}', "claude-hook")
    events = db.get_unprocessed()
    assert len(events) == 2
    assert events[0]["event_type"] == "boi.spec.completed"

def test_mark_processed(db):
    eid = db.insert_event("boi.spec.completed", '{}', "test")
    db.mark_processed(eid, "boi-complete")
    events = db.get_unprocessed()
    assert len(events) == 0

def test_log_action(db):
    eid = db.insert_event("test.event", '{}', "test")
    db.log_action(eid, "my-recipe", "shell", '{"cmd":"echo hi"}', "success")
    logs = db.get_action_logs(eid)
    assert len(logs) == 1
    assert logs[0]["status"] == "success"

def test_count_events(db):
    for i in range(5):
        db.insert_event("boi.spec.failed", '{}', "boi-daemon")
    count = db.count_events("boi.spec.failed", hours=1)
    assert count == 5

def test_history(db):
    db.insert_event("a.event", '{}', "src-a")
    db.insert_event("b.event", '{}', "src-b")
    history = db.history(limit=10)
    assert len(history) == 2

def test_janitor(db):
    # Insert old event (we'll test the SQL directly)
    eid = db.insert_event("old.event", '{}', "test")
    db.conn.execute("UPDATE events SET created_at = datetime('now', '-8 days') WHERE id = ?", (eid,))
    db.conn.commit()
    db.insert_event("new.event", '{}', "test")
    deleted = db.janitor(days=7)
    assert deleted == 1
    assert len(db.history(limit=10)) == 1
