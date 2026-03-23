#!/usr/bin/env python3
"""Poll calendar + Google Drive for new/updated AI meeting notes.

Emits a meeting.notes.detected event for each doc found. Dedup is handled
by hex-events via dedup_key = "meeting:{doc_id}:{modified_time}".

Two sources:
  A) Calendar: meetings that ended in the last hour with linked Google Docs
  B) Drive: docs titled "AI meeting notes" modified in the last hour

Requires: google-api-proxy, jf (Meta internal tools)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import EventsDB

DB_PATH = os.path.expanduser("~/.hex-events/events.db")
HEX_EMIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hex_emit.py")
LOOKBACK_SECONDS = 3600  # 1 hour


def poll_calendar_docs() -> list[dict]:
    """Query calendar for meetings with linked Google Docs."""
    user_email = f"{os.environ.get('USER', 'unknown')}@meta.com"
    now_epoch = int(time.time())
    lookback_epoch = now_epoch - LOOKBACK_SECONDS

    query = f"""{{
      calendar_meeting_events(fbid_or_email: "{user_email}", start: {lookback_epoch}, end: {now_epoch}) {{
        title
        start_time
        end_time
        knowledge_links {{ url title type }}
      }}
    }}"""

    try:
        result = subprocess.run(
            ["jf", "graphql", "--query", query],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []

    docs = []
    events = data.get("calendar_meeting_events") or []
    for event in events:
        end_time = event.get("end_time", 0)
        if end_time > now_epoch:
            continue  # meeting hasn't ended
        title = (event.get("title") or "").strip()
        for link in (event.get("knowledge_links") or []):
            url = link.get("url", "")
            m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
            if not m:
                m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
            if m:
                doc_id = m.group(1)
                mod_time = get_doc_modified_time(doc_id)
                docs.append({
                    "doc_id": doc_id,
                    "title": title,
                    "modified_time": mod_time or "",
                    "source": "calendar",
                })
    return docs


def poll_drive_zoom_notes() -> list[dict]:
    """Search Google Drive for recently modified Zoom AI meeting notes."""
    lookback_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Compute lookback time
    lookback_dt = datetime.fromtimestamp(
        time.time() - LOOKBACK_SECONDS, tz=timezone.utc
    )
    lookback_iso = lookback_dt.strftime("%Y-%m-%dT%H:%M:%S")

    query = (
        f"modifiedTime > '{lookback_iso}' "
        f"and mimeType = 'application/vnd.google-apps.document' "
        f"and name contains 'AI meeting notes'"
    )
    encoded_query = urllib.parse.quote(query)
    drive_url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q={encoded_query}"
        f"&fields=files(id,name,modifiedTime)"
        f"&pageSize=20"
        f"&orderBy=modifiedTime%20desc"
    )

    try:
        result = subprocess.run(
            ["google-api-proxy", "api", "call", "GET", drive_url],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []

    docs = []
    for f in data.get("files", []):
        doc_id = f.get("id", "")
        name = f.get("name", "")
        modified = f.get("modifiedTime", "")
        if doc_id:
            docs.append({
                "doc_id": doc_id,
                "title": name,
                "modified_time": modified,
                "source": "drive",
            })
    return docs


def get_doc_modified_time(doc_id: str) -> str:
    """Fetch a Google Doc's last modified timestamp via Drive API."""
    url = f"https://www.googleapis.com/drive/v3/files/{doc_id}?fields=modifiedTime"
    try:
        result = subprocess.run(
            ["google-api-proxy", "api", "call", "GET", url],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return ""
        data = json.loads(result.stdout)
        return data.get("modifiedTime", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return ""


def _dedup_key_exists(db: EventsDB, key: str) -> bool:
    """Check if any event with this dedup_key exists (processed or not).

    More restrictive than insert_event's built-in dedup (which only checks
    processed rows). Matches the scheduler adapter's approach.
    """
    row = db.conn.execute(
        "SELECT id FROM events WHERE dedup_key = ?", (key,)
    ).fetchone()
    return row is not None


def emit_event(doc: dict):
    """Emit a meeting.notes.detected event with dedup_key."""
    dedup_key = f"meeting:{doc['doc_id']}:{doc['modified_time']}"

    db = EventsDB(DB_PATH)

    # Check for ANY existing event with this key (processed or not)
    if _dedup_key_exists(db, dedup_key):
        db.close()
        print(f"Skipped (dedup): [{doc['doc_id']}] {doc['title']}")
        return

    payload = json.dumps({
        "doc_id": doc["doc_id"],
        "title": doc["title"],
        "modified_time": doc["modified_time"],
        "poll_source": doc["source"],
    })

    db.insert_event(
        "meeting.notes.detected",
        payload,
        "meeting-poll",
        dedup_key=dedup_key,
    )
    db.close()
    print(f"Emitted: meeting.notes.detected [{doc['doc_id']}] {doc['title']}")


def main():
    all_docs: dict[str, dict] = {}

    # Poll both sources
    for doc in poll_calendar_docs():
        all_docs[doc["doc_id"]] = doc

    for doc in poll_drive_zoom_notes():
        if doc["doc_id"] not in all_docs:
            all_docs[doc["doc_id"]] = doc

    if not all_docs:
        print("No meeting notes found")
        return

    print(f"Found {len(all_docs)} doc(s)")
    for doc in all_docs.values():
        emit_event(doc)


if __name__ == "__main__":
    main()
