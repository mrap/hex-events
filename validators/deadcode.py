"""Dead-code and naming validators for hex-events policies.

Checks:
- Duplicate rule names within a policy (DUPLICATE_RULE_NAME)
- Duplicate policy names across a corpus (DUPLICATE_POLICY_NAME)
- Unknown action types (UNKNOWN_ACTION_TYPE)
- Rule with no actions (NO_ACTIONS)
- Rate-limit window smaller than trigger cadence (RATE_LIMIT_CADENCE_MISMATCH, warning)
"""
import re
import yaml

# Known action types registered in actions/__init__.py
KNOWN_ACTION_TYPES = {"shell", "emit", "notify", "update-file", "dagu", "render"}

# Timer event name → period in seconds (statically known cadences)
_TIMER_CADENCES: dict[str, int] = {
    "timer.tick.minutely": 60,
    "timer.tick.1m": 60,
    "timer.tick.5m": 300,
    "timer.tick.15m": 900,
    "timer.tick.30m": 1800,
    "timer.tick.1h": 3600,
    "timer.tick.hourly": 3600,
    "timer.tick.2h": 7200,
    "timer.tick.4h": 14400,
    "timer.tick.6h": 21600,
    "timer.tick.daily": 86400,
    "timer.tick.daily.9am": 86400,
    "timer.tick.daily.6am": 86400,
    "timer.tick.weekly": 604800,
    "timer.tick.weekly-tick": 604800,
}

_WINDOW_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")
_WINDOW_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_window(window: str) -> int | None:
    """Parse a rate_limit window string like '50m', '1h', '2d' → seconds."""
    if not isinstance(window, str):
        return None
    m = _WINDOW_RE.match(window.strip())
    if not m:
        return None
    return int(m.group(1)) * _WINDOW_SECONDS[m.group(2)]


def validate(policy_path: str) -> list[dict]:
    """Per-policy dead-code checks (no corpus context required).

    Returns a list of issue dicts:
      {severity: "error"|"warning", code: str, message: str, location: {file, line}}
    """
    try:
        with open(policy_path) as f:
            doc = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return []

    if not isinstance(doc, dict):
        return []

    issues: list[dict] = []
    rules = doc.get("rules")
    if not isinstance(rules, list):
        return issues

    seen_rule_names: set[str] = set()

    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue

        rule_name = rule.get("name", f"<rule[{i}]>")

        # Duplicate rule names within this policy
        if isinstance(rule_name, str) and rule_name.strip():
            if rule_name in seen_rule_names:
                issues.append(_issue(
                    "error", "DUPLICATE_RULE_NAME",
                    f"Duplicate rule name '{rule_name}' within this policy",
                    policy_path, 1,
                ))
            else:
                seen_rule_names.add(rule_name)

        # Rule with no actions
        actions = rule.get("actions")
        if not isinstance(actions, list) or len(actions) == 0:
            issues.append(_issue(
                "error", "NO_ACTIONS",
                f"Rule '{rule_name}' has no actions — it will never do anything",
                policy_path, 1,
            ))
        else:
            # Unknown action types
            for j, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                atype = action.get("type")
                if isinstance(atype, str) and atype not in KNOWN_ACTION_TYPES:
                    issues.append(_issue(
                        "error", "UNKNOWN_ACTION_TYPE",
                        f"Rule '{rule_name}' actions[{j}]: unknown type '{atype}'. "
                        f"Known types: {', '.join(sorted(KNOWN_ACTION_TYPES))}",
                        policy_path, 1,
                    ))

        # Rate-limit cadence mismatch (warning, only when trigger is a known timer)
        trigger_evt = (rule.get("trigger") or {}).get("event")
        cadence_secs = _TIMER_CADENCES.get(trigger_evt) if trigger_evt else None
        if cadence_secs is not None:
            rl = doc.get("rate_limit") or {}
            window_str = rl.get("window")
            if window_str:
                window_secs = _parse_window(str(window_str))
                if window_secs is not None and window_secs < cadence_secs:
                    issues.append(_issue(
                        "warning", "RATE_LIMIT_CADENCE_MISMATCH",
                        f"Rule '{rule_name}': rate_limit.window ({window_str}) is shorter than "
                        f"trigger cadence ({trigger_evt} fires every {cadence_secs}s). "
                        "The rate limit will never be reached between firings.",
                        policy_path, 1,
                    ))

    return issues


def validate_corpus(policy_paths: list[str]) -> list[dict]:
    """Cross-policy checks that require seeing the full corpus.

    Checks:
    - Duplicate policy names across the corpus (DUPLICATE_POLICY_NAME)

    Returns a flat list of issue dicts annotated with the conflicting file path.
    """
    issues: list[dict] = []
    seen: dict[str, str] = {}  # policy name → first path

    for path in policy_paths:
        try:
            with open(path) as f:
                doc = yaml.safe_load(f)
        except (yaml.YAMLError, OSError):
            continue

        if not isinstance(doc, dict):
            continue

        policy_name = doc.get("name")
        if not isinstance(policy_name, str) or not policy_name.strip():
            continue

        if policy_name in seen:
            issues.append(_issue(
                "error", "DUPLICATE_POLICY_NAME",
                f"Policy name '{policy_name}' is already used by '{seen[policy_name]}'. "
                "Policy names must be unique across the corpus.",
                path, 1,
            ))
        else:
            seen[policy_name] = path

    return issues


def _issue(severity: str, code: str, message: str, filepath: str, line: int) -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "location": {"file": filepath, "line": line},
    }
