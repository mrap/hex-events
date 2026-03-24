# Daemon Singleton Enforcement

**Mode:** execute
**Target repo:** /Users/mrap/github.com/mrap/hex-events

The hex-events daemon (`hex_eventd.py`) has no protection against multiple instances running simultaneously. If you start it twice, both run and double-process events. It needs singleton enforcement: the second launch should exit cleanly.

The LaunchAgent already handles boot-start (`RunAtLoad=true`, `KeepAlive=true`), so this spec is only about the singleton lock.

## Context

- Daemon entry point: `hex_eventd.py`, function `run_daemon()`
- Docker test harness: `tests/Dockerfile.install` + `make smoke`
- Smoke test: `tests/smoke_test.sh` — already starts daemon as background process
- Test runner: `./venv/bin/python3 -m pytest tests/ -v --tb=short`
- Existing tests: `tests/` directory (22 files), plus root-level `test_*.py` files
- Use `fcntl.flock()` for the lock — auto-releases on crash/SIGKILL, no stale PID file problem

## Tasks

### t-1: Write failing integration test — two daemons, only one should survive (RED)
DONE

**Spec:** Create `tests/test_singleton.py`. This is a process-level integration test, not a unit test.

The test should:
1. Start `hex_eventd.py` as a subprocess (using `subprocess.Popen`)
2. Wait briefly for it to initialize (1-2 seconds)
3. Assert the first process is running (`poll() is None`)
4. Start a second `hex_eventd.py` as a subprocess
5. Wait for the second process to finish (it should exit quickly)
6. Assert the second process exited with code 0 (clean exit, not crash)
7. Assert the first process is still running
8. Count processes: only 1 daemon should be alive
9. Clean up: kill the first process

Use a temporary directory for `HEX_EVENTS_HOME` (or equivalent env var) so tests don't touch `~/.hex-events/`. If no env var override exists for the base dir, the test should set one up — check how `BASE_DIR` is defined in `hex_eventd.py` and use monkeypatch or env var accordingly.

Also write a second test:
- `test_daemon_starts_after_previous_exits` — start daemon, kill it, start another. The second one should succeed (lock released on death).

**Important:** These tests MUST fail right now because the singleton lock doesn't exist yet. The current daemon will happily run two instances.

Run: `./venv/bin/python3 -m pytest tests/test_singleton.py -v --tb=short`

**Verify:** Both tests fail. `./venv/bin/python3 -m pytest tests/test_singleton.py -v --tb=short 2>&1 | grep -c FAILED` returns 2. (The first test fails because both daemons stay alive; the second test may pass trivially — that's fine, at least 1 must fail.)

### t-2: Implement singleton lock in hex_eventd.py (GREEN)
DONE

**Spec:** Edit `hex_eventd.py` to add a PID file lock using `fcntl.flock()`:

1. Add `import fcntl` to imports
2. Add constant: `PID_FILE = os.path.join(BASE_DIR, "hex_eventd.pid")`
3. Add function `_acquire_singleton_lock()` that:
   - Opens `PID_FILE` for writing
   - Tries `fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)`
   - On `OSError`: log to stderr that another instance is running, close handle, `sys.exit(0)`
   - On success: write PID, flush, return the file handle
4. Call it as the first line of `run_daemon()`: `pid_fh = _acquire_singleton_lock()`
5. On shutdown (after `db.close()`): close `pid_fh`, remove `PID_FILE` (ignore errors), then log

Run: `./venv/bin/python3 -m pytest tests/test_singleton.py -v --tb=short`

**Verify:** `./venv/bin/python3 -m pytest tests/test_singleton.py -v --tb=short 2>&1 | grep -c PASSED` returns 2.

### t-3: Full regression — no existing tests broken
DONE

**Spec:** Run the complete test suite:
```bash
./venv/bin/python3 -m pytest tests/ test_daemon.py test_actions.py test_conditions.py test_db.py test_emit_cli.py test_recipe.py test_stress.py -v --tb=short
```

If any existing test fails, fix the regression without changing the singleton behavior.

**Verify:** `./venv/bin/python3 -m pytest tests/ test_daemon.py test_actions.py test_conditions.py test_db.py test_emit_cli.py test_recipe.py test_stress.py --tb=short 2>&1 | tail -1 | grep -qE 'passed|no tests ran'`

### t-4: Docker smoke test — clean room validation
DONE

**Spec:** Run `make smoke` to build from `tests/Dockerfile.install` and run the full smoke test in a clean container. This validates install + singleton in an isolated environment.

If the smoke test fails due to singleton-related issues (e.g., the smoke test starts the daemon twice and the second start now exits), fix the smoke test to account for the new behavior.

**Verify:** `make smoke 2>&1 | tail -5 | grep -qE 'PASS|passed'`

## Critic Approved

2026-03-23
