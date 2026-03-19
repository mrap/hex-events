"""Update file action (regex find/replace)."""
import os
import re
import tempfile
from jinja2 import Template
from actions import register

@register("update-file")
class UpdateFileAction:
    def run(self, params: dict, event_payload: dict, db=None) -> dict:
        target = params["target"]
        pattern = params["pattern"]
        replace = params["replace"]
        # Render templates
        if "{{" in target:
            target = Template(target).render(event=event_payload)
        if "{{" in pattern:
            pattern = Template(pattern).render(event=event_payload)
        if "{{" in replace:
            replace = Template(replace).render(event=event_payload)
        tmp_path = None
        try:
            with open(target) as f:
                content = f.read()
            new_content = re.sub(pattern, replace, content)
            target_dir = os.path.dirname(os.path.abspath(target))
            with tempfile.NamedTemporaryFile(mode="w", dir=target_dir, delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(new_content)
            os.replace(tmp_path, target)
            changed = content != new_content
            return {"status": "success", "changed": changed}
        except Exception as e:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return {"status": "error", "output": str(e)}
