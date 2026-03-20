"""Shell command action plugin."""
import subprocess
from jinja2 import Template
from actions import register

@register("shell")
class ShellAction:
    def _run_sub_actions(self, sub_actions: list, event_payload: dict,
                         action_result: dict, db=None):
        """Execute on_success or on_failure sub-actions."""
        from actions import get_action_handler

        for raw in (sub_actions or []):
            atype = raw.get("type")
            handler = get_action_handler(atype)
            if not handler:
                continue
            params = {}
            for k, v in raw.items():
                if k == "type":
                    continue
                if isinstance(v, str) and "{{" in v:
                    params[k] = Template(v).render(event=event_payload,
                                                   action=action_result)
                elif isinstance(v, dict):
                    rendered = {}
                    for dk, dv in v.items():
                        if isinstance(dv, str) and "{{" in dv:
                            rendered[dk] = Template(dv).render(
                                event=event_payload, action=action_result)
                        else:
                            rendered[dk] = dv
                    params[k] = rendered
                else:
                    params[k] = v
            handler.run(params, event_payload=event_payload, db=db)

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
                action_result = {"stdout": result.stdout.strip(), "returncode": 0}
                self._run_sub_actions(params.get("on_success"), event_payload,
                                      action_result, db=db)
                return {"status": "success", "output": result.stdout.strip()}
            else:
                error_output = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                action_result = {"stderr": error_output,
                                 "returncode": result.returncode}
                self._run_sub_actions(params.get("on_failure"), event_payload,
                                      action_result, db=db)
                return {"status": "error", "output": error_output,
                        "code": result.returncode}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": "timeout after 60s"}
