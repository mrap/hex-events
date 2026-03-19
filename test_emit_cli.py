# test_emit_cli.py
import json
import os
import tempfile
import subprocess
import pytest
from db import EventsDB

@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)

def test_emit_inserts_event(db_path):
    """Test hex-emit inserts an event into the database."""
    venv_python = os.path.expanduser("~/.hex-events/venv/bin/python")
    emit_script = os.path.expanduser("~/.hex-events/hex_emit.py")
    result = subprocess.run(
        [venv_python, emit_script, "--db", db_path, "test.event", '{"key":"val"}', "test-source"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    db = EventsDB(db_path)
    events = db.history(limit=1)
    assert len(events) == 1
    assert events[0]["event_type"] == "test.event"
    assert json.loads(events[0]["payload"])["key"] == "val"
    db.close()
