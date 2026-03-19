"""Emit event action (recipe chaining). Supports delayed emit + cancel_group."""
import json
from datetime import datetime, timedelta

from actions import register


@register("emit")
class EmitAction:
    def run(self, params: dict, event_payload: dict, db=None) -> dict:
        if not params.get("event"):
            return {"status": "error", "output": "emit action missing required 'event' parameter"}

        event_type = params["event"]
        payload = params.get("payload", {})
        if isinstance(payload, str) and "{{" in payload:
            try:
                from jinja2 import Template
                payload = json.loads(Template(payload).render(event=event_payload))
            except Exception as e:
                return {"status": "error", "output": f"Template render failed: {e}"}
        delay = params.get("delay")
        cancel_group = params.get("cancel_group")

        if delay is not None:
            from db import parse_duration
            seconds = parse_duration(delay)
            if seconds > 0:
                # Deferred emit: write to deferred_events table
                if db is None:
                    return {"status": "error", "output": "Cannot defer event: no database connection"}
                fire_at = (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()
                db.insert_deferred(
                    event_type,
                    json.dumps(payload),
                    "policy-emit",
                    fire_at,
                    cancel_group=cancel_group,
                )
                return {"status": "success", "emitted": event_type, "deferred": True,
                        "delay": delay}
            # delay == 0: fall through to immediate emit

        # Immediate emit
        if db:
            db.insert_event(event_type, json.dumps(payload), "recipe-emit")
        return {"status": "success", "emitted": event_type}
