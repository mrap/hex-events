# db.py
"""SQLite event bus for hex-events."""
import json
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    processed_at TEXT,
    recipe TEXT
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    recipe TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_detail TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    executed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events(processed_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""

class EventsDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def insert_event(self, event_type: str, payload: str, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (event_type, payload, source) VALUES (?, ?, ?)",
            (event_type, payload, source),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_unprocessed(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, event_id: int, recipe: str | None = None):
        self.conn.execute(
            "UPDATE events SET processed_at = datetime('now'), recipe = ? WHERE id = ?",
            (recipe, event_id),
        )
        self.conn.commit()

    def log_action(self, event_id: int, recipe: str, action_type: str,
                   action_detail: str, status: str, error_message: str | None = None):
        self.conn.execute(
            "INSERT INTO action_log (event_id, recipe, action_type, action_detail, status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, recipe, action_type, action_detail, status, error_message),
        )
        self.conn.commit()

    def get_action_logs(self, event_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM action_log WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_events(self, event_type: str, hours: int = 1) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE event_type = ? AND created_at >= datetime('now', ?)",
            (event_type, f"-{hours} hours"),
        ).fetchone()
        return row["cnt"]

    def history(self, limit: int = 50, since_hours: int | None = None) -> list[dict]:
        if since_hours:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                (f"-{since_hours} hours", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def janitor(self, days: int = 7) -> int:
        cur = self.conn.execute(
            "DELETE FROM events WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        self.conn.execute(
            "DELETE FROM action_log WHERE event_id NOT IN (SELECT id FROM events)"
        )
        self.conn.commit()
        return cur.rowcount
