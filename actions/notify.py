"""Notification action (delegates to hex-notify.sh)."""
import os
import subprocess
from jinja2 import Template
from actions import register

@register("notify")
class NotifyAction:
    def run(self, params: dict, event_payload: dict, db=None) -> dict:
        message = params["message"]
        if "{{" in message:
            message = Template(message).render(event=event_payload)
        try:
            result = subprocess.run(
                ["bash", os.path.expanduser("~/.claude/scripts/hex-notify.sh"), message],
                capture_output=True, text=True, timeout=30,
            )
            return {"status": "success" if result.returncode == 0 else "error", "output": result.stdout.strip()}
        except Exception as e:
            return {"status": "error", "output": str(e)}
