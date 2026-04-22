"""Schema validator for hex-events policies.

Validates that a policy YAML file conforms to the canonical form:
  name, description, optional requires/provides/rate_limit, then rules:[...]
  where each rule has: name, trigger:{event:<str>}, actions:[...]

Rejects the legacy flat form (top-level trigger: + action:).
"""
import yaml


def validate(policy_path: str) -> list[dict]:
    """Validate a policy file against the canonical schema.

    Returns a list of issue dicts:
      {severity: "error"|"warning", code: str, message: str, location: {file, line}}
    """
    try:
        with open(policy_path) as f:
            raw = f.read()
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return [_issue("error", "YAML_PARSE_ERROR", f"YAML parse error: {e}", policy_path, 1)]
    except OSError as e:
        return [_issue("error", "FILE_READ_ERROR", f"Cannot read file: {e}", policy_path, 1)]

    if not isinstance(doc, dict):
        return [_issue("error", "NOT_A_DICT", "Policy file must be a YAML mapping at top level", policy_path, 1)]

    issues = []

    # Detect rejected flat form: top-level trigger: + action: (singular)
    if "trigger" in doc or "action" in doc:
        issues.append(_issue(
            "error",
            "SCHEMA_FLAT_FORM",
            "Policy uses deprecated flat form (top-level 'trigger:'/'action:' keys). "
            "Use canonical form: rules:[{name, trigger:{event:...}, actions:[...]}]. "
            "See integrations/_template/events/ for a canonical example.",
            policy_path, 1,
        ))
        return issues  # flat-form policies can't be meaningfully validated further

    # name must be a non-empty string
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(_issue("error", "MISSING_NAME", "Top-level 'name' must be a non-empty string", policy_path, 1))

    # rules must be a non-empty list
    rules = doc.get("rules")
    if not isinstance(rules, list) or len(rules) == 0:
        issues.append(_issue("error", "MISSING_RULES", "'rules' must be a non-empty list", policy_path, 1))
        return issues

    seen_rule_names: set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            issues.append(_issue("error", "RULE_NOT_DICT", f"rules[{i}] must be a mapping", policy_path, 1))
            continue

        rule_name = rule.get("name")
        if not isinstance(rule_name, str) or not rule_name.strip():
            issues.append(_issue(
                "error", "RULE_MISSING_NAME",
                f"rules[{i}] missing or empty 'name'",
                policy_path, 1,
            ))
            rule_name = f"<rule[{i}]>"
        elif rule_name in seen_rule_names:
            issues.append(_issue(
                "error", "DUPLICATE_RULE_NAME",
                f"Duplicate rule name '{rule_name}' within this policy",
                policy_path, 1,
            ))
        else:
            seen_rule_names.add(rule_name)

        trigger = rule.get("trigger")
        if not isinstance(trigger, dict):
            issues.append(_issue(
                "error", "RULE_MISSING_TRIGGER",
                f"Rule '{rule_name}': 'trigger' must be a mapping with an 'event' key",
                policy_path, 1,
            ))
        else:
            event = trigger.get("event")
            if not isinstance(event, str) or not event.strip():
                issues.append(_issue(
                    "error", "RULE_TRIGGER_NO_EVENT",
                    f"Rule '{rule_name}': trigger.event must be a non-empty string",
                    policy_path, 1,
                ))

        actions = rule.get("actions")
        if not isinstance(actions, list) or len(actions) == 0:
            issues.append(_issue(
                "error", "NO_ACTIONS",
                f"Rule '{rule_name}': 'actions' must be a non-empty list",
                policy_path, 1,
            ))
        else:
            for j, action in enumerate(actions):
                if not isinstance(action, dict):
                    issues.append(_issue(
                        "error", "ACTION_NOT_DICT",
                        f"Rule '{rule_name}' actions[{j}] must be a mapping",
                        policy_path, 1,
                    ))
                    continue
                if "type" not in action:
                    issues.append(_issue(
                        "error", "ACTION_MISSING_TYPE",
                        f"Rule '{rule_name}' actions[{j}] missing required 'type' field",
                        policy_path, 1,
                    ))

    return issues


def _issue(severity: str, code: str, message: str, filepath: str, line: int) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "location": {"file": filepath, "line": line},
    }
