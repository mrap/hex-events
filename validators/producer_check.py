"""Producer-check validator for hex-events policies.

Cross-references each rule's trigger event and requires.events against the
event catalog to detect subscriptions with no producer.
"""
import yaml


def validate(policy_path: str, catalog: dict) -> list[dict]:
    """Check that every subscribed event has at least one producer.

    Args:
        policy_path: Path to the policy YAML file.
        catalog: Event catalog dict as returned by _build_event_catalog().
                 Keys are event names; values are {producers: [...], consumers: [...]}.

    Returns a list of issue dicts:
      {severity: "error"|"warning", code: str, message: str, location: {file, line}}
    """
    try:
        with open(policy_path) as f:
            doc = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        # Parse errors are already reported by the schema validator; skip here.
        return []

    if not isinstance(doc, dict):
        return []

    issues: list[dict] = []
    checked: set[str] = set()

    def _check_event(event: str, context: str) -> None:
        if not isinstance(event, str) or not event.strip():
            return
        if event in checked:
            return
        checked.add(event)

        entry = catalog.get(event)
        producers = entry["producers"] if entry else []
        if not producers:
            issues.append(_issue(
                "error",
                "EVENT_NO_PRODUCER",
                f"{context}: subscribes to '{event}' but no producer emits it. "
                "Add a scheduler entry or a policy emit action, or remove the subscription.",
                policy_path,
                1,
            ))
        elif len(producers) > 1:
            prod_names = ", ".join(
                p.get("name", str(p)) for p in producers
            )
            issues.append(_issue(
                "warning",
                "EVENT_MULTIPLE_PRODUCERS",
                f"{context}: event '{event}' has {len(producers)} producers ({prod_names}). "
                "Multiple producers are allowed but may indicate redundancy.",
                policy_path,
                1,
            ))

    # requires.events — declared consumed events
    for evt in (doc.get("requires") or {}).get("events", []):
        _check_event(evt, "requires.events")

    # Each rule's trigger event
    for rule in doc.get("rules", []):
        if not isinstance(rule, dict):
            continue
        rule_name = rule.get("name", "<unnamed rule>")
        trigger_evt = (rule.get("trigger") or {}).get("event")
        if trigger_evt:
            _check_event(trigger_evt, f"rule '{rule_name}' trigger")

    return issues


def _issue(severity: str, code: str, message: str, filepath: str, line: int) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "location": {"file": filepath, "line": line},
    }
