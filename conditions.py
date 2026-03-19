# conditions.py
"""Condition evaluator for hex-events recipes."""
import re
from policy import Condition
from db import parse_duration

# Matches count(event_type, duration) where duration is like 10m, 1h, 2d, 30s, or bare int
COUNT_RE = re.compile(r"^count\(([^,]+),\s*(\d+[smhd]?)\)$")


def evaluate_conditions(conditions: list[Condition], payload: dict, db) -> bool:
    """Evaluate all conditions (AND logic). Returns True if all pass."""
    if not conditions:
        return True
    for cond in conditions:
        if not _evaluate_one(cond, payload, db):
            return False
    return True


def _evaluate_one(cond: Condition, payload: dict, db) -> bool:
    # Check for count() function
    m = COUNT_RE.match(cond.field)
    if m:
        event_type, duration_str = m.group(1), m.group(2)
        if db is None:
            return False
        seconds = parse_duration(duration_str)
        actual = db.count_events(event_type, seconds=seconds)
    else:
        actual = payload.get(cond.field)
        if actual is None:
            return False

    op = cond.op
    expected = cond.value

    if op == "eq":
        return actual == expected
    elif op == "neq":
        return actual != expected
    elif op == "gt":
        return actual > expected
    elif op == "gte":
        return actual >= expected
    elif op == "lt":
        return actual < expected
    elif op == "lte":
        return actual <= expected
    elif op == "contains":
        return str(expected) in str(actual)
    else:
        return False
