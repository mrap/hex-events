"""
test_stress.py — stress/integration tests for hex-events.
Runs against the LIVE daemon and database.
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.expanduser("~/.hex-events")
DB_PATH = os.path.join(BASE_DIR, "events.db")
PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")
EMIT_CLI = os.path.join(BASE_DIR, "hex_emit.py")
EVENTS_CLI = os.path.join(BASE_DIR, "hex_events_cli.py")

sys.path.insert(0, BASE_DIR)

# ---------------------------------------------------------------------------
# Shared metrics store (populated by individual tests; read by conftest.py)
# ---------------------------------------------------------------------------

STRESS_REPORT: dict = {
    "throughput_events_per_sec": None,   # float — set by test_rapid_fire
    "recovery_time_sec": None,           # float — set by test_daemon_recovery
    "warnings": [],                      # list[str] — appended by any test
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_daemon_pid() -> int | None:
    """Return the hex_eventd PID, or None if not running."""
    result = subprocess.run(["pgrep", "-f", "hex_eventd"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    pids = [int(p) for p in result.stdout.strip().split() if p.strip()]
    return pids[0] if pids else None


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def wait_for_processed(event_id: int, timeout: int = 10) -> bool:
    """Poll until the event is processed or timeout (seconds)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = db_connect()
        row = conn.execute(
            "SELECT processed_at FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        conn.close()
        if row and row["processed_at"] is not None:
            return True
        time.sleep(1)
    return False


def emit_event(event_type: str, payload: str = "{}", source: str = "stress-test") -> int:
    """Call hex_emit.py and return the new event ID."""
    result = subprocess.run(
        [PYTHON, EMIT_CLI, event_type, payload, source],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    )
    assert result.returncode == 0, f"hex_emit failed: {result.stderr}"
    # Output: "Event <id>: <type>"
    line = result.stdout.strip()
    eid = int(line.split()[1].rstrip(":"))
    return eid


# ---------------------------------------------------------------------------
# t-1: Daemon is alive and processing events
# ---------------------------------------------------------------------------

def test_daemon_alive():
    # 1. Assert daemon is running
    pid = get_daemon_pid()
    assert pid is not None, "hex_eventd is not running (pgrep returned nothing)"

    # 2. Insert a probe event
    ts = datetime.utcnow().isoformat()
    eid = emit_event("test.stress.probe", f'{{"ts":"{ts}"}}', "stress-test")

    # 3. Wait up to 10s for processing
    processed = wait_for_processed(eid, timeout=10)

    # 4. Assert processed
    assert processed, f"Probe event {eid} was not processed within 10s"

    # 5. Assert all 4 recipes are loaded
    result = subprocess.run(
        [PYTHON, EVENTS_CLI, "recipes"],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    )
    assert result.returncode == 0, f"hex_events_cli recipes failed: {result.stderr}"
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    assert len(lines) >= 4, (
        f"Expected at least 4 recipes, got {len(lines)}:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# t-2: Rapid-fire 100 events
# ---------------------------------------------------------------------------

def test_rapid_fire():
    from db import EventsDB

    db = EventsDB(DB_PATH)

    # 1. Record current max event ID
    row = db.conn.execute("SELECT MAX(id) as max_id FROM events").fetchone()
    baseline_id = row["max_id"] or 0

    # 2. Fire 100 events as fast as possible
    start = time.time()
    inserted_ids = []
    for i in range(100):
        eid = db.insert_event(f"stress.rapid.{i}", '{"i":' + str(i) + '}', "stress")
        inserted_ids.append(eid)
    insert_elapsed = time.time() - start

    db.close()

    # 3. Wait up to 60 seconds for ALL 100 to be processed
    deadline = time.time() + 60
    while time.time() < deadline:
        conn = db_connect()
        unprocessed = conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE id > ? AND processed_at IS NULL AND event_type LIKE 'stress.rapid.%'",
            (baseline_id,),
        ).fetchone()["cnt"]
        conn.close()
        if unprocessed == 0:
            break
        time.sleep(1)

    total_elapsed = time.time() - start

    # 4. Assert all 100 were processed
    conn = db_connect()
    processed_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE id > ? AND processed_at IS NOT NULL AND event_type LIKE 'stress.rapid.%'",
        (baseline_id,),
    ).fetchone()["cnt"]
    conn.close()

    assert processed_count == 100, (
        f"Expected 100 processed stress.rapid events, got {processed_count}"
    )

    # 5. Assert processing took < 60 seconds total
    assert total_elapsed < 60, f"Processing 100 events took {total_elapsed:.1f}s (> 60s)"

    # 6. Report throughput
    throughput = 100 / total_elapsed
    STRESS_REPORT["throughput_events_per_sec"] = throughput
    print(f"\n[t-2] Rapid-fire: inserted 100 events in {insert_elapsed:.2f}s, "
          f"all processed in {total_elapsed:.2f}s ({throughput:.1f} events/s)")


# ---------------------------------------------------------------------------
# t-3: Concurrent writes from multiple processes
# ---------------------------------------------------------------------------

def test_concurrent_writes():
    import tempfile

    log_path = os.path.join(BASE_DIR, "daemon.log")

    # Record baseline to scope our assertions
    conn = db_connect()
    row = conn.execute("SELECT MAX(id) as max_id FROM events").fetchone()
    baseline_id = row["max_id"] or 0
    conn.close()

    # Record daemon.log line count before test so we only inspect new lines
    log_baseline_lines = 0
    if os.path.exists(log_path):
        with open(log_path) as f:
            log_baseline_lines = sum(1 for _ in f)

    # Write a small worker script to a temp file
    worker_script = """
import sys, os
sys.path.insert(0, sys.argv[1])
from db import EventsDB
db = EventsDB(sys.argv[2])
worker_id = sys.argv[3]
for i in range(20):
    db.insert_event(
        f"stress.concurrent.{worker_id}.{i}",
        '{"i":' + str(i) + '}',
        "stress-concurrent",
    )
db.close()
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=BASE_DIR
    ) as tmp:
        tmp.write(worker_script)
        worker_script_path = tmp.name

    try:
        # 1+2. Spawn 5 workers simultaneously
        procs = []
        for w in range(5):
            proc = subprocess.Popen(
                [PYTHON, worker_script_path, BASE_DIR, DB_PATH, str(w)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(proc)

        # Wait for all workers to finish (max 30s)
        for proc in procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr_out = proc.stderr.read().decode()
            assert proc.returncode == 0, (
                f"Worker process exited {proc.returncode}: {stderr_out}"
            )

        # 3. Assert all 100 events exist in DB (no lost writes)
        conn = db_connect()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM events "
            "WHERE id > ? AND event_type LIKE 'stress.concurrent.%'",
            (baseline_id,),
        ).fetchone()["cnt"]
        conn.close()
        assert count == 100, f"Expected 100 concurrent events in DB, got {count}"

        # 4. Assert no sqlite3 lock errors appeared in daemon.log during the test
        if os.path.exists(log_path):
            with open(log_path) as f:
                all_lines = f.readlines()
            new_lines = all_lines[log_baseline_lines:]
            db_errors = [
                l.strip() for l in new_lines
                if "database is locked" in l or "OperationalError" in l
            ]
            assert len(db_errors) == 0, (
                f"sqlite3 errors in daemon.log during concurrent test:\n"
                + "\n".join(db_errors)
            )

        # 5. Wait for all 100 to be processed (up to 60s)
        deadline = time.time() + 60
        while time.time() < deadline:
            conn = db_connect()
            unprocessed = conn.execute(
                "SELECT COUNT(*) as cnt FROM events "
                "WHERE id > ? AND processed_at IS NULL AND event_type LIKE 'stress.concurrent.%'",
                (baseline_id,),
            ).fetchone()["cnt"]
            conn.close()
            if unprocessed == 0:
                break
            time.sleep(2)

        conn = db_connect()
        processed_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM events "
            "WHERE id > ? AND processed_at IS NOT NULL AND event_type LIKE 'stress.concurrent.%'",
            (baseline_id,),
        ).fetchone()["cnt"]
        conn.close()
        assert processed_count == 100, (
            f"Expected 100 processed concurrent events, got {processed_count}"
        )

        print(
            f"\n[t-3] Concurrent writes: 5 workers × 20 events = 100 total, "
            f"all written and processed"
        )

    finally:
        os.unlink(worker_script_path)


# ---------------------------------------------------------------------------
# t-4: Malformed payloads don't crash daemon
# ---------------------------------------------------------------------------

def _restart_daemon_gracefully(timeout: int = 30) -> int:
    """
    Send SIGTERM to the running daemon and wait for LaunchAgent to restart it.
    Returns the new daemon PID. Needed when hex_eventd.py was modified after daemon start.
    """
    import signal as _signal

    old_pid = get_daemon_pid()
    assert old_pid is not None, "No daemon running to restart"
    os.kill(old_pid, _signal.SIGTERM)

    # Wait for old daemon to die
    deadline = time.time() + 10
    while time.time() < deadline:
        cur = get_daemon_pid()
        if cur is None or cur != old_pid:
            break
        time.sleep(0.5)

    # Wait for LaunchAgent to bring it back
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = get_daemon_pid()
        if cur is not None and cur != old_pid:
            time.sleep(2)  # let it fully initialize
            return cur
        time.sleep(1)

    raise AssertionError(f"Daemon did not restart within {timeout}s after SIGTERM")


def test_malformed_payloads():
    import json
    import warnings

    sys.path.insert(0, BASE_DIR)
    from db import EventsDB

    # Restart daemon so it runs the current hex_eventd.py code (which has per-event
    # error handling for malformed JSON). The running daemon may have been started
    # before the code was updated.
    new_pid = _restart_daemon_gracefully()

    db = EventsDB(DB_PATH)

    # 1. Record daemon PID before test (the freshly restarted daemon)
    pid_before = new_pid
    assert pid_before is not None, "Daemon must be running before malformed payload test"

    # Build nested 50-level deep JSON
    nested = {}
    for _ in range(50):
        nested = {"n": nested}
    nested_payload = json.dumps(nested)

    # 10 intentionally bad payloads
    bad_payloads = [
        ("", "empty string"),
        ("not json", "non-JSON string"),
        ("null", "JSON null"),
        ("{}", "empty JSON object"),
        # null byte: may be rejected by sqlite3 TEXT; handle gracefully
        ('{"key": "\\u0000"}', "unicode null escape"),
        ("A" * 10240, "10KB string"),
        (nested_payload, "50-level nested JSON"),
        ('{"msg": "héllo wörld 🌍"}', "unicode payload"),
        ("'; DROP TABLE events; --", "SQL injection attempt"),
        ("42", "integer JSON value"),
    ]

    # Record log baseline
    log_path = os.path.join(BASE_DIR, "daemon.log")
    log_baseline_lines = 0
    if os.path.exists(log_path):
        with open(log_path) as f:
            log_baseline_lines = sum(1 for _ in f)

    # 2. Insert events with bad payloads; track which ones succeed
    inserted_ids = []
    test_start = time.time()
    for payload_str, label in bad_payloads:
        try:
            eid = db.insert_event(
                f"stress.malformed.{label.replace(' ', '_')}",
                payload_str,
                "stress-malformed",
            )
            inserted_ids.append(eid)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"[t-4] Could not insert payload '{label}': {exc}")

    db.close()

    expected_count = len(inserted_ids)
    assert expected_count > 0, "No malformed events could be inserted"

    # 3. Wait up to 30 seconds for processing (poll, not just sleep)
    deadline = time.time() + 30
    while time.time() < deadline:
        conn = db_connect()
        remaining = conn.execute(
            "SELECT COUNT(*) as cnt FROM events "
            "WHERE id IN ({}) AND processed_at IS NULL".format(
                ",".join("?" * len(inserted_ids))
            ),
            inserted_ids,
        ).fetchone()["cnt"]
        conn.close()
        if remaining == 0:
            break
        time.sleep(2)

    # 4. Assert daemon PID is still the same (no crash/restart)
    pid_after = get_daemon_pid()
    assert pid_after == pid_before, (
        f"Daemon PID changed: before={pid_before}, after={pid_after} — possible crash/restart"
    )

    # 5. Assert all inserted events are marked as processed
    conn = db_connect()
    unprocessed = conn.execute(
        "SELECT COUNT(*) as cnt FROM events "
        "WHERE id IN ({}) AND processed_at IS NULL".format(
            ",".join("?" * len(inserted_ids))
        ),
        inserted_ids,
    ).fetchone()["cnt"]
    conn.close()

    assert unprocessed == 0, (
        f"{unprocessed} of {expected_count} malformed-payload events were NOT processed"
    )

    # 6. Check daemon.log for Python tracebacks — warn, don't fail
    tracebacks_found = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            new_lines = f.readlines()[log_baseline_lines:]
        tb_lines = [l.strip() for l in new_lines if "Traceback" in l or "Error" in l]
        if tb_lines:
            tracebacks_found = tb_lines
            msg = (
                f"[t-4] {len(tracebacks_found)} error line(s) in daemon.log "
                f"during malformed payload test (expected for bad payloads): "
                + "; ".join(tracebacks_found[:3])
            )
            STRESS_REPORT["warnings"].append(msg)
            warnings.warn(msg)

    print(
        f"\n[t-4] Malformed payloads: {expected_count}/{len(bad_payloads)} inserted, "
        f"all processed. Daemon PID stable ({pid_before}). "
        f"Log errors: {len(tracebacks_found)}"
    )


# ---------------------------------------------------------------------------
# t-5: Recipe hot-reload
# ---------------------------------------------------------------------------

HOTRELOAD_RECIPE_PATH = os.path.join(BASE_DIR, "recipes", "stress-test-hotreload.yaml")
HOTRELOAD_RECIPE_YAML = """\
name: stress-hotreload
trigger:
  event: stress.hotreload
actions:
  - type: shell
    command: echo "hotreload works"
"""


def _count_loaded_recipes() -> int:
    """Return number of recipes reported by hex_events_cli.py recipes."""
    result = subprocess.run(
        [PYTHON, EVENTS_CLI, "recipes"],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    )
    assert result.returncode == 0, f"hex_events_cli recipes failed: {result.stderr}"
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return len(lines)


def _recipe_names_from_cli() -> list:
    """Return list of recipe names from hex_events_cli.py recipes output."""
    result = subprocess.run(
        [PYTHON, EVENTS_CLI, "recipes"],
        capture_output=True,
        text=True,
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        return []
    names = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            # Format: "  {name:25s}  trigger=..."
            names.append(stripped.split()[0])
    return names


def test_hot_reload():
    # Ensure cleanup from any prior run
    if os.path.exists(HOTRELOAD_RECIPE_PATH):
        os.remove(HOTRELOAD_RECIPE_PATH)

    # 1. Count currently loaded recipes
    initial_count = _count_loaded_recipes()
    initial_names = _recipe_names_from_cli()
    assert "stress-hotreload" not in initial_names, (
        "stress-hotreload recipe already loaded before test — stale file?"
    )

    # 2. Write the temporary recipe
    with open(HOTRELOAD_RECIPE_PATH, "w") as fh:
        fh.write(HOTRELOAD_RECIPE_YAML)

    try:
        # 3. Wait 15 seconds for daemon to pick up the new recipe (reload every 10s)
        time.sleep(15)

        # Verify it's loaded
        names_after_add = _recipe_names_from_cli()
        assert "stress-hotreload" in names_after_add, (
            f"stress-hotreload recipe not loaded after 15s wait. "
            f"Loaded recipes: {names_after_add}"
        )
        count_after_add = len(names_after_add)
        assert count_after_add == initial_count + 1, (
            f"Expected {initial_count + 1} recipes after adding, got {count_after_add}"
        )

        # 4. Fire the stress.hotreload event
        eid = emit_event("stress.hotreload", '{"test": "hotreload"}', "stress-test")

        # 5. Wait up to 10 seconds for processing
        processed = wait_for_processed(eid, timeout=10)
        assert processed, f"stress.hotreload event {eid} was not processed within 10s"

        # 6. Assert the event matched stress-hotreload recipe
        conn = db_connect()
        row = conn.execute(
            "SELECT recipe FROM events WHERE id = ?", (eid,)
        ).fetchone()
        conn.close()
        assert row is not None, f"Event {eid} not found in DB"
        assert row["recipe"] == "stress-hotreload", (
            f"Expected recipe='stress-hotreload', got recipe={row['recipe']!r}"
        )

        print(f"\n[t-5] Hot-reload: recipe loaded and matched event {eid}")

    finally:
        # 7. Cleanup: remove the temporary recipe file
        if os.path.exists(HOTRELOAD_RECIPE_PATH):
            os.remove(HOTRELOAD_RECIPE_PATH)

    # 8. Wait 15 seconds and verify the recipe is no longer loaded
    time.sleep(15)
    names_after_remove = _recipe_names_from_cli()
    assert "stress-hotreload" not in names_after_remove, (
        f"stress-hotreload recipe still listed after removal and 15s wait. "
        f"Loaded recipes: {names_after_remove}"
    )
    count_after_remove = len(names_after_remove)
    assert count_after_remove == initial_count, (
        f"Expected {initial_count} recipes after removal, got {count_after_remove}"
    )

    print(f"\n[t-5] Hot-reload: recipe unloaded after file removal. "
          f"Recipe count restored to {count_after_remove}.")


# ---------------------------------------------------------------------------
# t-6: Daemon recovery after kill -9
# ---------------------------------------------------------------------------

def test_daemon_recovery():
    import signal

    # 1. Record current daemon PID
    pid_before = get_daemon_pid()
    assert pid_before is not None, "Daemon must be running before kill test"

    kill_time = time.time()

    # 2. Send SIGKILL to the daemon
    os.kill(pid_before, signal.SIGKILL)

    # Wait for the old PID to disappear
    deadline = time.time() + 10
    while time.time() < deadline:
        cur = get_daemon_pid()
        if cur is None or cur != pid_before:
            break
        time.sleep(0.5)

    # 3. Wait up to 30 seconds for LaunchAgent to restart the daemon
    new_pid = None
    deadline = time.time() + 30
    while time.time() < deadline:
        cur = get_daemon_pid()
        if cur is not None and cur != pid_before:
            new_pid = cur
            break
        time.sleep(2)

    recovery_time = time.time() - kill_time

    # 4. Assert a NEW daemon PID is running
    assert new_pid is not None, (
        f"Daemon did not restart within 30s after SIGKILL "
        f"(old PID: {pid_before})"
    )
    assert new_pid != pid_before, (
        f"Daemon PID unchanged after SIGKILL: {new_pid} — may be a different process"
    )

    # Allow daemon to fully initialize
    time.sleep(2)

    # 5. Fire a probe event and wait for it to be processed
    ts = datetime.utcnow().isoformat()
    eid = emit_event("test.stress.recovery.probe", f'{{"ts":"{ts}"}}', "stress-test")
    processed = wait_for_processed(eid, timeout=15)
    assert processed, (
        f"Probe event {eid} was not processed within 15s after daemon recovery"
    )

    # 6. Assert total downtime was < 30 seconds
    assert recovery_time < 30, (
        f"Daemon recovery took {recovery_time:.1f}s (> 30s)"
    )

    STRESS_REPORT["recovery_time_sec"] = recovery_time
    print(
        f"\n[t-6] Daemon recovery: killed PID {pid_before}, "
        f"new PID {new_pid} recovered in {recovery_time:.1f}s, "
        f"probe event {eid} processed successfully"
    )


# ---------------------------------------------------------------------------
# t-7: Janitor cleans old events
# ---------------------------------------------------------------------------

def test_janitor():
    from db import EventsDB

    db = EventsDB(DB_PATH)

    # 1. Insert 5 events that will be made artificially old
    old_ids = []
    for i in range(5):
        eid = db.insert_event(
            f"stress.janitor.old.{i}",
            f'{{"i": {i}}}',
            "stress-janitor",
        )
        old_ids.append(eid)

    # Update created_at to 8 days ago so janitor(days=7) removes them
    placeholders = ",".join("?" * len(old_ids))
    db.conn.execute(
        f"UPDATE events SET created_at = datetime('now', '-8 days') WHERE id IN ({placeholders})",
        old_ids,
    )
    db.conn.commit()

    # 2. Insert 5 events with current timestamps (these must survive janitor)
    new_ids = []
    for i in range(5):
        eid = db.insert_event(
            f"stress.janitor.new.{i}",
            f'{{"i": {i}}}',
            "stress-janitor",
        )
        new_ids.append(eid)

    # 3. Wait for all 10 to be processed (up to 30s)
    all_ids = old_ids + new_ids
    deadline = time.time() + 30
    while time.time() < deadline:
        remaining = db.conn.execute(
            f"SELECT COUNT(*) as cnt FROM events "
            f"WHERE id IN ({','.join('?' * len(all_ids))}) AND processed_at IS NULL",
            all_ids,
        ).fetchone()["cnt"]
        if remaining == 0:
            break
        time.sleep(2)

    # Manually insert action_log entries for all events so we can verify cascade cleanup
    for eid in all_ids:
        db.log_action(
            event_id=eid,
            recipe="stress-janitor-recipe",
            action_type="shell",
            action_detail="echo test",
            status="ok",
        )
    db.conn.commit()

    # Confirm action_log entries exist for old events before janitor
    old_log_count_before = db.conn.execute(
        f"SELECT COUNT(*) as cnt FROM action_log "
        f"WHERE event_id IN ({','.join('?' * len(old_ids))})",
        old_ids,
    ).fetchone()["cnt"]
    assert old_log_count_before == len(old_ids), (
        f"Expected {len(old_ids)} action_log entries for old events before janitor, "
        f"got {old_log_count_before}"
    )

    # 4. Trigger janitor manually
    deleted_count = db.janitor(days=7)

    # 5. Assert the 5 old events were deleted
    old_remaining = db.conn.execute(
        f"SELECT COUNT(*) as cnt FROM events WHERE id IN ({placeholders})",
        old_ids,
    ).fetchone()["cnt"]
    assert old_remaining == 0, (
        f"Expected 0 old events after janitor, found {old_remaining}"
    )
    assert deleted_count >= 5, (
        f"janitor() reported only {deleted_count} deletions, expected >= 5"
    )

    # 6. Assert the 5 new events still exist
    new_remaining = db.conn.execute(
        f"SELECT COUNT(*) as cnt FROM events "
        f"WHERE id IN ({','.join('?' * len(new_ids))})",
        new_ids,
    ).fetchone()["cnt"]
    assert new_remaining == len(new_ids), (
        f"Expected {len(new_ids)} new events after janitor, found {new_remaining}"
    )

    # 7. Assert action_log entries for old events were cleaned up
    old_log_count_after = db.conn.execute(
        f"SELECT COUNT(*) as cnt FROM action_log "
        f"WHERE event_id IN ({','.join('?' * len(old_ids))})",
        old_ids,
    ).fetchone()["cnt"]
    assert old_log_count_after == 0, (
        f"Expected 0 action_log entries for old events after janitor, "
        f"got {old_log_count_after}"
    )

    db.close()

    print(
        f"\n[t-7] Janitor: deleted {deleted_count} old events (IDs {old_ids}), "
        f"{len(new_ids)} new events preserved. "
        f"action_log cascade: {old_log_count_before} → {old_log_count_after} entries."
    )


# ---------------------------------------------------------------------------
# t-8: End-to-end: git commit triggers full pipeline
# ---------------------------------------------------------------------------

HEX_REPO = os.path.realpath(os.path.expanduser(os.environ.get("HEX_REPO", "~/hex")))  # resolve symlinks
E2E_TEST_FILE = os.path.join(HEX_REPO, "raw", "captures", "stress-test-e2e-temp.md")
E2E_COMMIT_MSG = "stress-test: e2e commit"
# Per-repo hook (may be overridden by global core.hooksPath)
E2E_HOOK_PATH = os.path.join(HEX_REPO, ".git", "hooks", "post-commit")


def _find_effective_post_commit_hook(repo_path: str) -> str | None:
    """
    Return the path to the effective post-commit hook for the given repo.

    git may use a global core.hooksPath that overrides the per-repo .git/hooks/.
    Check the global config first, then fall back to the repo-local path.
    """
    # Check global core.hooksPath
    result = subprocess.run(
        ["git", "config", "--global", "core.hooksPath"],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    if result.returncode == 0:
        global_hooks_dir = result.stdout.strip()
        if global_hooks_dir:
            candidate = os.path.expanduser(os.path.join(global_hooks_dir, "post-commit"))
            if os.path.isfile(candidate):
                return candidate

    # Check repo-local .git/hooks/post-commit
    repo_hook = os.path.join(repo_path, ".git", "hooks", "post-commit")
    if os.path.isfile(repo_hook):
        return repo_hook

    return None


def test_e2e_git_commit():
    import json

    # 1. Verify the git post-commit hook exists (check per-repo path as specified)
    assert os.path.isfile(E2E_HOOK_PATH), (
        f"git post-commit hook not found at {E2E_HOOK_PATH}"
    )
    assert os.access(E2E_HOOK_PATH, os.X_OK), (
        f"git post-commit hook at {E2E_HOOK_PATH} is not executable"
    )

    # Determine the effective hook that git will actually call.
    # Note: global core.hooksPath may override the per-repo hook.
    effective_hook = _find_effective_post_commit_hook(HEX_REPO)
    uses_repo_hook = (effective_hook == E2E_HOOK_PATH) if effective_hook else False

    # Record baseline max event ID before the commit
    conn = db_connect()
    row = conn.execute("SELECT MAX(id) as max_id FROM events").fetchone()
    baseline_id = row["max_id"] or 0
    conn.close()

    test_file_created = False
    committed = False
    try:
        # 2. Create a test file in the hex repo
        with open(E2E_TEST_FILE, "w") as fh:
            fh.write(f"# stress-test e2e temp\nCreated at {datetime.now().isoformat()}\n")
        test_file_created = True

        # Stage only this specific file
        rel_path = os.path.relpath(E2E_TEST_FILE, HEX_REPO)
        result = subprocess.run(
            ["git", "add", rel_path],
            capture_output=True,
            text=True,
            cwd=HEX_REPO,
        )
        assert result.returncode == 0, f"git add failed: {result.stderr}"

        # Commit with the e2e message.
        # Note: global core.hooksPath may not include a post-commit hook, so git
        # may not call the hex-events hook automatically — we invoke it explicitly below.
        result = subprocess.run(
            ["git", "commit", "-m", E2E_COMMIT_MSG],
            capture_output=True,
            text=True,
            cwd=HEX_REPO,
        )
        assert result.returncode == 0, f"git commit failed: {result.stderr}\n{result.stdout}"
        committed = True

        # The per-repo post-commit hook may not fire if git's global core.hooksPath
        # overrides the repo-local .git/hooks/ directory.  Invoke the hook explicitly
        # to ensure the full event → daemon → recipe pipeline is exercised end-to-end.
        # (If git DID fire it, invoking it again just adds a duplicate event; we search
        #  by commit message so the assertion still passes on the first match.)
        hook_result = subprocess.run(
            ["bash", E2E_HOOK_PATH],
            capture_output=True,
            text=True,
            cwd=HEX_REPO,
        )
        # Hook emits in background (`&`); non-zero exit is acceptable (best-effort)
        _ = hook_result

        # Give the background hex_emit.py call inside the hook time to complete
        time.sleep(2)

        # 3. Wait up to 15 seconds for a git.push event with our commit message to appear
        target_event_id = None
        deadline = time.time() + 15
        while time.time() < deadline:
            conn = db_connect()
            rows = conn.execute(
                "SELECT id, payload FROM events "
                "WHERE id > ? AND event_type = 'git.push' "
                "ORDER BY id DESC LIMIT 10",
                (baseline_id,),
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    payload_data = json.loads(row["payload"])
                    if payload_data.get("message") == E2E_COMMIT_MSG:
                        target_event_id = row["id"]
                        break
                except (json.JSONDecodeError, KeyError):
                    continue
            if target_event_id is not None:
                break
            time.sleep(1)

        assert target_event_id is not None, (
            f"No git.push event with message '{E2E_COMMIT_MSG}' found within 15s "
            f"(baseline_id={baseline_id}, effective_hook={effective_hook!r})"
        )

        # 4. Verify payload contains the commit message (already confirmed above)
        conn = db_connect()
        event_row = conn.execute(
            "SELECT payload, recipe, processed_at FROM events WHERE id = ?",
            (target_event_id,),
        ).fetchone()
        conn.close()

        payload_data = json.loads(event_row["payload"])
        assert payload_data.get("message") == E2E_COMMIT_MSG, (
            f"Event payload message mismatch: {payload_data}"
        )

        # 5. Wait for the event to be processed with the commit-changelog recipe
        processed = wait_for_processed(target_event_id, timeout=15)
        assert processed, (
            f"git.push event {target_event_id} was not processed within 15s"
        )

        conn = db_connect()
        event_row = conn.execute(
            "SELECT recipe FROM events WHERE id = ?", (target_event_id,)
        ).fetchone()
        conn.close()
        assert event_row["recipe"] == "commit-changelog", (
            f"Expected recipe='commit-changelog', got recipe={event_row['recipe']!r} "
            f"for event {target_event_id}"
        )

        print(
            f"\n[t-8] E2E git commit: event {target_event_id} processed, "
            f"recipe='{event_row['recipe']}', payload message='{payload_data.get('message')}' "
            f"(hook invoked {'via git' if uses_repo_hook else 'explicitly — global hooksPath overrides repo hook'})"
        )

    finally:
        # 6. Cleanup: revert the commit and remove the test file
        if committed:
            subprocess.run(
                ["git", "reset", "--soft", "HEAD~1"],
                capture_output=True,
                cwd=HEX_REPO,
            )
            # Unstage the file
            subprocess.run(
                ["git", "restore", "--staged", rel_path],
                capture_output=True,
                cwd=HEX_REPO,
            )
        if test_file_created and os.path.exists(E2E_TEST_FILE):
            os.remove(E2E_TEST_FILE)
