"""Shell command action plugin."""
import subprocess
from jinja2 import Template
from actions import register

@register("shell")
class ShellAction:
    def run(self, params: dict, event_payload: dict, db=None) -> dict:
        command = params["command"]
        # Render Jinja2 templates in command
        if "{{" in command:
            command = Template(command).render(event=event_payload)
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                return {"status": "success", "output": result.stdout.strip()}
            else:
                return {"status": "error", "output": result.stderr.strip(), "code": result.returncode}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": "timeout after 60s"}
