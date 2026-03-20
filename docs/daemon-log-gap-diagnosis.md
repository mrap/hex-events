# Daemon Log Gap Diagnosis

**Date:** 2026-03-19  
**Investigator:** BOI worker (q-162, t-1)  
**Symptom:** `~/.hex-events/daemon.log` stopped receiving entries at 01:12 on 2026-03-19. Events continued processing (events.db last modified 16:08).

---

## Root Cause

**The launchd service is NOT loaded.** The running daemon (PID 30106) was started manually by a previous BOI worker session, with stdout/stderr captured by a Claude task temp file — not by `daemon.log`.

### Evidence

| Check | Finding |
|-------|---------|
| `launchctl list com.mrap.hex-eventd` | "service not found" — service is unloaded |
| `lsof -p 30106` FD 1 (stdout) | `/private/tmp/claude-502/.../bxks033jl.output` |
| `lsof -p 30106` FD 2 (stderr) | Same Claude task output file |
| daemon.log last modified | `Mar 19 01:12` — when launchd-managed daemon stopped |
| events.db last modified | `Mar 19 16:08` — daemon IS processing events |

### Timeline

1. **2026-03-19 01:12** — Launchd-managed daemon received SIGTERM and stopped (last log: "hex-eventd stopped").
2. **Something caused `launchctl unload`** — The service was unloaded (not just the process killed). With `KeepAlive: true`, launchd would normally restart — but `unload` prevents that.
3. **~11:47AM** — A subsequent BOI worker session (boi-worker-4) manually started the daemon via `python hex_eventd.py`. Its stdout/stderr go to a temp Claude task file, not `daemon.log`.
4. **Now** — Daemon is alive and processing events, but logging is invisible to daemon.log.

---

## Checklist Answers

### 1. Is the LaunchAgent plist pointing StandardOutPath/StandardErrorPath to daemon.log?
**Yes.** `/Users/mrap/Library/LaunchAgents/com.mrap.hex-eventd.plist` correctly configures:
```xml
<key>StandardOutPath</key>
<string>/Users/mrap/.hex-events/daemon.log</string>
<key>StandardErrorPath</key>
<string>/Users/mrap/.hex-events/daemon.log</string>
```
This is correct — when launchd manages the process, both streams go to daemon.log.

### 2. Is the daemon using Python's `logging` module or just `print()`?
**`logging` module.** `hex_eventd.py` calls `logging.basicConfig()` with no explicit handler, defaulting to a `StreamHandler` writing to `sys.stderr`. This works correctly when launchd sets up the file descriptors — but is fragile when started manually.

### 3. After a daemon restart (SIGTERM + relaunch), does logging reinitialize?
**Yes** — each new Python process calls `run_daemon()` → `logging.basicConfig()` fresh. No stale handler state. However, this only works if launchd relaunches the process (KeepAlive), NOT if the service is unloaded first.

### 4. Is there a log rotation issue (file handle goes stale after restart)?
**No.** The current logging approach writes to `sys.stderr` which inherits the fd opened by launchd on each restart. There's no `FileHandler` with a stale fd. Log rotation is not in use.

### 5. LaunchAgent plist log path config
Confirmed above — plist is correctly configured but the **service is not loaded**.

---

## Secondary Issues (for t-2 to fix)

1. **Fragile logging architecture** — relying entirely on launchd fd inheritance means any manual daemon start (e.g., by a BOI worker, or for debugging) produces invisible logs.
2. **No startup log** — there is a startup log line (`hex-eventd starting (pid=XXXX)`) but it only appears if logging is working.
3. **No heartbeat** — no periodic health signal in daemon.log, so a silent daemon is indistinguishable from a dead one.
4. **No direct FileHandler** — switching to `logging.handlers.RotatingFileHandler` writing directly to `~/.hex-events/daemon.log` would make logging independent of how the daemon was launched.
5. **stdout buffering risk** — `print()` calls (e.g., "New capture:" lines from actions) are fully buffered when stdout is a file. Should flush or use `-u` flag.

---

## Recommended Fix (for t-2)

Replace `logging.basicConfig()` + implicit stderr with an explicit `RotatingFileHandler`:

```python
import logging.handlers

LOG_FILE = os.path.join(BASE_DIR, "daemon.log")

handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
)
handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)
```

This ensures:
- Logging works regardless of how the daemon was started
- Log rotation prevents unbounded growth
- File handle survives process restarts (new process opens new handle)
- Independent of launchd fd inheritance
