"""Shell command action plugin."""
import subprocess
from jinja2 import Template
from actions import register

@register("shell")
class ShellAction:
    def _run_sub_actions(self, sub_actions: list, event_payload: dict,
                         action_result: dict, db=None, workflow_context=None):
        """Execute on_success or on_failure sub-actions."""
        from actions import get_action_handler

        tpl_ctx = {"event": event_payload, "action": action_result}
        if workflow_context:
            tpl_ctx["workflow"] = workflow_context

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
                    params[k] = Template(v).render(**tpl_ctx)
                elif isinstance(v, dict):
                    rendered = {}
                    for dk, dv in v.items():
                        if isinstance(dv, str) and "{{" in dv:
                            rendered[dk] = Template(dv).render(**tpl_ctx)
                        else:
                            rendered[dk] = dv
                    params[k] = rendered
                else:
                    params[k] = v
            handler.run(params, event_payload=event_payload, db=db,
                        workflow_context=workflow_context)

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
                self._run_sub_actions(params.get("on_success"), event_payload,
                                      action_result, db=db,
                                      workflow_context=workflow_context)
                return {"status": "success", "output": result.stdout.strip()}
            else:
                error_output = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
                action_result = {"stderr": error_output,
                                 "returncode": result.returncode}
                self._run_sub_actions(params.get("on_failure"), event_payload,
                                      action_result, db=db,
                                      workflow_context=workflow_context)
                return {"status": "error", "output": error_output,
                        "code": result.returncode}
        except subprocess.TimeoutExpired:
            return {"status": "error", "output": f"timeout after {timeout}s"}
