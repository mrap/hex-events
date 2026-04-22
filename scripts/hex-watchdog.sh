#!/bin/bash
# hex-watchdog.sh — Independent health monitor for hex-events and BOI.
# No Python, no hex-events dependency. Pure bash + sqlite3.
# Runs via LaunchAgent every 60 seconds.
# No set -e: individual checks should not abort the entire watchdog.
set -uo pipefail

LOG="${HOME}/.hex-events/watchdog.log"
EVENTS_DB="${HOME}/.hex-events/events.db"
PAUSE_MARKER="${HOME}/.hex-events/watchdog-paused"
INTEGRITY_MARKER="${HOME}/.hex-events/watchdog-last-integrity"
CASCADE_THRESHOLD=20
STALL_THRESHOLD_SECONDS=300

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }
alert() { osascript -e "display notification \"$1\" with title \"hex-watchdog\"" 2>/dev/null || true; }

# 1. hex-events daemon
if ! pgrep -f "hex_eventd.py" > /dev/null 2>&1; then
    log "ALERT: hex-events daemon not running. Attempting restart."
    alert "hex-events daemon is down. Restarting..."
    launchctl kickstart -k "gui/$(id -u)/com.hex.eventd" 2>/dev/null || {
        log "ERROR: Failed to restart hex-events daemon via launchctl"
        alert "hex-events daemon restart FAILED"
    }
else
    log "OK: hex-events daemon running"
fi

# 2. BOI daemon
if ! pgrep -f "boi/.*daemon.py" > /dev/null 2>&1; then
    log "ALERT: BOI daemon not running"
    alert "BOI daemon is down. Manual investigation required."
else
    log "OK: BOI daemon running"
fi

# 3. Event processing stall
if [[ -f "$EVENTS_DB" ]]; then
    stalled=$(sqlite3 "$EVENTS_DB" \
        "SELECT COUNT(*) FROM events WHERE processed_at IS NULL AND created_at <= datetime('now', '-${STALL_THRESHOLD_SECONDS} seconds');" 2>/dev/null || echo "0")
    if [[ "$stalled" -gt 0 ]]; then
        log "ALERT: $stalled events stalled (unprocessed > ${STALL_THRESHOLD_SECONDS}s)"
        alert "$stalled events stalled in hex-events"
    else
        log "OK: No stalled events"
    fi

    # 4. Cascade detection (exclude boi.* — expected high-volume events from parallel BOI workers)
    cascade=$(sqlite3 "$EVENTS_DB" \
        "SELECT event_type, COUNT(*) as cnt FROM events WHERE created_at >= datetime('now', '-10 minutes') AND event_type NOT LIKE 'boi.%' GROUP BY event_type HAVING cnt > $CASCADE_THRESHOLD;" 2>/dev/null || echo "")
    if [[ -n "$cascade" ]]; then
        log "ALERT: Event cascade detected: $cascade"
        alert "Event cascade detected! Pausing hex-events daemon."
        daemon_pid=$(pgrep -f "hex_eventd.py" 2>/dev/null | head -1)
        if [[ -n "$daemon_pid" ]]; then
            kill -SIGSTOP "$daemon_pid" 2>/dev/null || true
            echo "$(date '+%Y-%m-%d %H:%M:%S') Cascade: $cascade" > "$PAUSE_MARKER"
            log "ACTION: Paused hex-events daemon (SIGSTOP pid=$daemon_pid)"
        fi
    else
        log "OK: No cascade"
    fi

    # 5. WAL size check
    if [[ -f "${EVENTS_DB}-wal" ]]; then
        wal_size=$(stat -f%z "${EVENTS_DB}-wal" 2>/dev/null || echo "0")
        wal_mb=$((wal_size / 1048576))
        if [[ "$wal_mb" -gt 100 ]]; then
            log "ALERT: WAL file is ${wal_mb}MB (threshold: 100MB)"
            alert "hex-events WAL file is ${wal_mb}MB"
        else
            log "OK: WAL size ${wal_mb}MB"
        fi
    fi

    # 6. Integrity check (hourly)
    now=$(date +%s)
    last_integrity=0
    [[ -f "$INTEGRITY_MARKER" ]] && last_integrity=$(cat "$INTEGRITY_MARKER" 2>/dev/null || echo "0")
    elapsed=$((now - last_integrity))
    if [[ "$elapsed" -ge 3600 ]]; then
        integrity=$(sqlite3 "$EVENTS_DB" "PRAGMA integrity_check;" 2>/dev/null || echo "error")
        if [[ "$integrity" != "ok" ]]; then
            log "ALERT: Database integrity check failed: $integrity"
            alert "hex-events database integrity check FAILED"
        else
            log "OK: Database integrity check passed"
        fi
        echo "$now" > "$INTEGRITY_MARKER"
    fi
else
    log "WARN: events.db not found at $EVENTS_DB"
fi
