"""Contract validator for hex-events policies.

Checks cross-component event contracts:
- DEAD_TRIGGER: a trigger.event has no known producer → ERROR
- ORPHAN_EVENT: an emitted event has no policy trigger consuming it → WARNING

System events (timer.*, sys.*) are always considered valid producers.
"""
import os
import re
import yaml

# Events from these prefixes are system-generated — never dead triggers.
_SYSTEM_PREFIXES = ("timer.", "sys.", "system.", "hex.system.")

# Regex to find hex_emit.py calls in shell/Python scripts.
# Require either a quoted argument or a dotted event name to avoid
# matching prose like "hex_emit.py is not installed".
#   python3 ... hex_emit.py event.name.here
#   python3 ... hex_emit.py "event.name.here"
_SHELL_EMIT_RE = re.compile(
    r'hex_emit\.py\s+(?:'
    r'["\']([a-z][a-z0-9._-]+)["\']'  # quoted
    r'|([a-z][a-z0-9._-]*\.[a-z][a-z0-9._-]+)'  # unquoted with dot
    r')'
)
# Python emit(event_type, ...) calls — require at least one dot to avoid
# matching Python keywords like emit("not", ...) or emit("is", ...)
#   emit("hex.foo.bar", ...)
_PY_EMIT_RE = re.compile(
    r'\bemit\s*\(\s*["\']([a-z][a-z0-9._-]*\.[a-z][a-z0-9._-]+)["\']'
)


def _is_system_event(event: str) -> bool:
    return any(event.startswith(p) for p in _SYSTEM_PREFIXES)


def _collect_policy_contracts(policy_paths: list) -> tuple:
    """Extract triggers and emitters from policy YAML files.

    Returns:
        triggers:  {event: [(policy_name, rule_name), ...]}
        emitters:  {event: [(source_label, rule_name), ...]}
    """
    triggers: dict = {}
    emitters: dict = {}

    for path in policy_paths:
        try:
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue

        policy_name = doc.get("name") or os.path.basename(path)

        for rule in doc.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            rule_name = rule.get("name", "")

            # trigger.event → consumer
            trigger_evt = (rule.get("trigger") or {}).get("event")
            if trigger_evt and isinstance(trigger_evt, str):
                triggers.setdefault(trigger_evt, []).append((policy_name, rule_name))

            # actions with type=emit → producer
            for action in rule.get("actions", []) or []:
                if not isinstance(action, dict):
                    continue
                if action.get("type") == "emit":
                    evt = action.get("event")
                    if evt and isinstance(evt, str):
                        emitters.setdefault(evt, []).append(
                            (f"policy:{policy_name}", rule_name)
                        )

            # on_success / on_failure emit hooks → producers
            for hook in ("on_success", "on_failure"):
                for action in rule.get(hook, []) or []:
                    if isinstance(action, dict) and action.get("type") == "emit":
                        evt = action.get("event")
                        if evt and isinstance(evt, str):
                            emitters.setdefault(evt, []).append(
                                (f"policy:{policy_name}/{hook}", rule_name)
                            )

    return triggers, emitters


def _collect_script_emitters(scripts_dirs: list) -> dict:
    """Scan .sh and .py files for hex_emit.py calls."""
    emitters: dict = {}

    for directory in scripts_dirs:
        if not os.path.isdir(directory):
            continue
        for fname in os.listdir(directory):
            if not (fname.endswith(".sh") or fname.endswith(".py")):
                continue
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, errors="replace") as fh:
                    content = fh.read()
            except Exception:
                continue

            for m in _SHELL_EMIT_RE.finditer(content):
                evt = m.group(1) or m.group(2)
                if evt:
                    emitters.setdefault(evt, []).append((f"script:{fname}", ""))
            for m in _PY_EMIT_RE.finditer(content):
                evt = m.group(1)
                emitters.setdefault(evt, []).append((f"script:{fname}", ""))

    return emitters


def _collect_scheduler_events(scheduler_config: str) -> set:
    """Return the set of events produced by the scheduler adapter."""
    events: set = set()
    if not os.path.exists(scheduler_config):
        return events
    try:
        with open(scheduler_config) as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return events
    for sched in data.get("schedules", []):
        evt = sched.get("event")
        if evt:
            events.add(evt)
    return events


def validate_corpus(policy_paths: list, scripts_dirs: list = None,
                    scheduler_config: str = None) -> list:
    """Run contract checks across all policies + scripts.

    Returns a flat list of issue dicts:
      {severity, code, message, location: {file, line}}
    """
    if scripts_dirs is None:
        scripts_dirs = []

    if scheduler_config is None:
        base = os.path.expanduser("~/.hex-events")
        scheduler_config = os.path.join(base, "adapters", "scheduler.yaml")

    triggers, policy_emitters = _collect_policy_contracts(policy_paths)
    script_emitters = _collect_script_emitters(scripts_dirs)
    scheduler_events = _collect_scheduler_events(scheduler_config)

    # Merge all emitters
    all_emitters: dict = {}
    for evt, sources in policy_emitters.items():
        all_emitters.setdefault(evt, []).extend(sources)
    for evt, sources in script_emitters.items():
        all_emitters.setdefault(evt, []).extend(sources)

    issues: list = []

    # --- Dead triggers: consumed but never produced ---
    for event in sorted(triggers):
        if _is_system_event(event):
            continue
        if event in scheduler_events:
            continue
        if event not in all_emitters:
            consumers = triggers[event]
            consumer_str = ", ".join(
                f"{p} rule '{r}'" if r else p for p, r in consumers[:3]
            )
            if len(consumers) > 3:
                consumer_str += f" (+{len(consumers) - 3} more)"
            issues.append({
                "severity": "error",
                "code": "DEAD_TRIGGER",
                "message": (
                    f"contract: dead trigger — '{event}' is triggered by {consumer_str} "
                    f"but no emitter found in policies or scripts"
                ),
                "location": {"file": consumers[0][0], "line": 1},
            })

    # --- Orphan events: emitted but never consumed ---
    for event in sorted(all_emitters):
        if event not in triggers:
            producers = all_emitters[event]
            producer_str = ", ".join(
                p for p, _ in producers[:3]
            )
            if len(producers) > 3:
                producer_str += f" (+{len(producers) - 3} more)"
            issues.append({
                "severity": "warning",
                "code": "ORPHAN_EVENT",
                "message": (
                    f"contract: orphan event — '{event}' is emitted by {producer_str} "
                    f"but no policy trigger consumes it"
                ),
                "location": {"file": producers[0][0], "line": 1},
            })

    return issues
