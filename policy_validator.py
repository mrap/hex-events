"""Policy validation for hex-events."""
import yaml

VALID_ACTION_TYPES = {"shell", "emit", "notify", "update-file"}
VALID_CONDITION_OPS = {"eq", "neq", "contains", "gt", "lt", "gte", "lte", "glob", "regex"}


def validate_policy(policy: dict, filename: str = "<unknown>") -> list[str]:
    """Validate a policy dict against the hex-events schema.
    Returns list of error strings. Empty list = valid."""
    errors = []

    if not isinstance(policy.get("name"), str):
        errors.append(f"{filename}: missing or invalid 'name' (must be a string)")

    rules = policy.get("rules")
    if not isinstance(rules, list) or len(rules) == 0:
        errors.append(f"{filename}: missing or empty 'rules' (must be a non-empty list)")
        return errors  # can't validate rules if they don't exist

    for rule in rules:
        rule_name = rule.get("name", "<unnamed>") if isinstance(rule, dict) else "<unnamed>"
        prefix = f"{filename} rule '{rule_name}'"

        if not isinstance(rule, dict):
            errors.append(f"{filename}: rule is not a dict")
            continue

        if not isinstance(rule.get("name"), str):
            errors.append(f"{prefix}: missing or invalid 'name' (must be a string)")

        trigger = rule.get("trigger")
        if not isinstance(trigger, dict):
            errors.append(f"{prefix}: missing or invalid 'trigger' (must be a dict)")
        else:
            if not isinstance(trigger.get("event"), str):
                errors.append(f"{prefix}: trigger missing 'event' (must be a string)")

        actions = rule.get("actions")
        if not isinstance(actions, list) or len(actions) == 0:
            errors.append(f"{prefix}: missing or empty 'actions' (must be a non-empty list)")
        else:
            for i, action in enumerate(actions):
                action_prefix = f"{prefix} action[{i}]"
                if not isinstance(action, dict):
                    errors.append(f"{action_prefix}: action is not a dict")
                    continue

                atype = action.get("type")
                if atype not in VALID_ACTION_TYPES:
                    errors.append(
                        f"{action_prefix}: invalid type '{atype}' "
                        f"(expected: {', '.join(sorted(VALID_ACTION_TYPES))})"
                    )
                elif atype == "shell" and not isinstance(action.get("command"), str):
                    errors.append(f"{action_prefix}: shell action missing 'command' (must be a string)")
                elif atype == "emit" and not isinstance(action.get("event"), str):
                    errors.append(f"{action_prefix}: emit action missing 'event' (must be a string)")

        condition = rule.get("condition")
        if condition is not None:
            if not isinstance(condition, dict):
                errors.append(f"{prefix}: 'condition' must be a dict")
            else:
                if not isinstance(condition.get("field"), str):
                    errors.append(f"{prefix}: condition missing 'field' (must be a string)")
                op = condition.get("op")
                if op not in VALID_CONDITION_OPS:
                    errors.append(
                        f"{prefix}: condition.op '{op}' is not valid "
                        f"(expected: {', '.join(sorted(VALID_CONDITION_OPS))})"
                    )
                if "value" not in condition:
                    errors.append(f"{prefix}: condition missing 'value'")

    return errors


def validate_policy_file(filepath: str) -> list[str]:
    """Read a YAML policy file and validate it. Returns list of error strings."""
    try:
        with open(filepath) as f:
            policy = yaml.safe_load(f)
    except Exception as e:
        return [f"{filepath}: failed to parse YAML: {e}"]

    if not isinstance(policy, dict):
        return [f"{filepath}: policy file must be a YAML dict, got {type(policy).__name__}"]

    return validate_policy(policy, filepath)
