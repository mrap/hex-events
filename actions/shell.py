"""Shell command action plugin."""
import subprocess
from jinja2 import Template
from actions import register

@register("shell")
class ShellAction:
    def run(self, params: dict, event_payload: dict, db=None,
            workflow_context=None) -> dict:
        command = params["command"]
        timeout = int(params.get("timeout", 60))
        # Render Jinja2 templates in command
        if "{{" in command:
            tpl_ctx = {"event": event_payload}
            if workflow_context:
                tpl_ctx["workflow"] = workflow_context
            command = Template(command).render(**tpl_ctx)
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                action_result = {"stdout": result.stdout.strip(), "returncode": 0}
                return {"status": "success", "output": result.stdout.strip(),
                        "_action_result": action_result}
            else:
                error_output = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                action_result = {"stderr": error_output,
                                 "returncode": result.returncode}
                return {"status": "error", "output": error_output,
                        "code": result.returncode,
                        "_action_result": action_result}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": f"timeout after {timeout}s"}
