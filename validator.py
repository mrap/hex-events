# validator.py
"""Static graph validator and observed event graph for hex-events v2.

Provides:
  - build_static_graph: construct event dependency graph from policies + adapters
  - validate_graph: check satisfiability, orphans, and cycles
  - get_observed_events: query DB for real event/policy data
  - compare_graphs: diff static vs observed event sets
"""
import os

import yaml

BASE_DIR = os.path.expanduser("~/.hex-events")
DEFAULT_POLICIES_DIR = os.path.join(BASE_DIR, "policies")
DEFAULT_RECIPES_DIR = os.path.join(BASE_DIR, "recipes")
DEFAULT_SCHEDULER_CONFIG = os.path.join(BASE_DIR, "adapters", "scheduler.yaml")


# ---------------------------------------------------------------------------
# Adapter events
# ---------------------------------------------------------------------------

def load_adapter_events(config_path: str = DEFAULT_SCHEDULER_CONFIG) -> set:
    """Return the set of event types emitted by the scheduler adapter."""
    if not os.path.exists(config_path):
        return set()
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        schedules = data.get("schedules", []) or []
        return {s["event"] for s in schedules if "event" in s}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Static graph construction
# ---------------------------------------------------------------------------

def build_static_graph(policies: list, adapter_events: set | None = None) -> dict:
    """Build a static event dependency graph from policies and adapter events.

    Returns a dict:
      provided_by  : event_type -> list[provider_name]  (policy name or "@adapter:scheduler")
      required_by  : event_type -> list[policy_name]    (policies that trigger on this event)
      edges        : list[(trigger_event, provided_event, policy_name)]
      adapter_events: set of event types provided by adapters
    """
    if adapter_events is None:
        adapter_events = set()

    provided_by: dict = {}
    required_by: dict = {}
    edges: list = []

    # Adapter events are "provided" by the scheduler adapter
    for evt in adapter_events:
        provided_by.setdefault(evt, []).append("@adapter:scheduler")

    for policy in policies:
        trigger_events = {rule.trigger_event for rule in policy.rules}

        provides_events = list(policy.provides.get("events", []))
        requires_events = list(policy.requires.get("events", []))

        # Fall back to rule trigger events if no explicit requires
        if not requires_events:
            requires_events = list(trigger_events)

        for evt in provides_events:
            provided_by.setdefault(evt, []).append(policy.name)

        for evt in requires_events:
            required_by.setdefault(evt, []).append(policy.name)

        # Edges: requires → provides (via this policy)
        for trigger in requires_events:
            for provided in provides_events:
                edges.append((trigger, provided, policy.name))

    return {
        "provided_by": provided_by,
        "required_by": required_by,
        "edges": edges,
        "adapter_events": adapter_events,
    }


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def _detect_cycles(adjacency: dict) -> list:
    """Detect cycles in a directed graph using DFS.

    adjacency: dict[node, set[neighbor]]
    Returns a list of cycle paths (each path is a list of nodes ending at the
    repeated node to show the loop clearly).
    """
    all_nodes: set = set(adjacency.keys())
    for v in adjacency.values():
        all_nodes |= set(v)

    visited: set = set()
    rec_stack: set = set()
    cycles: list = []

    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in adjacency.get(node, set()):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                # Found a back edge → cycle
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])

        path.pop()
        rec_stack.discard(node)

    for node in all_nodes:
        if node not in visited:
            dfs(node, [])

    return cycles


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------

def validate_graph(graph: dict) -> dict:
    """Validate the event dependency graph.

    Checks:
      unsatisfied    : requires events with no provider → error
      orphan_provides: provided events with no consumer → warning
      cycles         : cycles in the event graph → error

    Returns:
      valid      : bool — True only if unsatisfied and cycles are empty
      unsatisfied: list[{"event": str, "required_by": list[str]}]
      orphan_provides: list[{"event": str, "provided_by": list[str]}]
      cycles     : list[list[str]]
    """
    provided_by = graph["provided_by"]
    required_by = graph["required_by"]
    edges = graph["edges"]

    all_provided = set(provided_by.keys())
    all_required = set(required_by.keys())

    # Events required but not provided by anyone
    unsatisfied = []
    for evt in sorted(all_required):
        if evt not in all_provided:
            unsatisfied.append({
                "event": evt,
                "required_by": sorted(required_by[evt]),
            })

    # Events provided but never triggered on by any policy
    orphan_provides = []
    for evt in sorted(all_provided):
        if evt not in all_required:
            orphan_provides.append({
                "event": evt,
                "provided_by": sorted(provided_by[evt]),
            })

    # Build adjacency list: trigger_event → set of provided_events
    adjacency: dict = {}
    for (trigger, provided, _policy) in edges:
        adjacency.setdefault(trigger, set()).add(provided)

    cycles = _detect_cycles(adjacency)

    return {
        "valid": not unsatisfied and not cycles,
        "unsatisfied": unsatisfied,
        "orphan_provides": orphan_provides,
        "cycles": cycles,
    }


# ---------------------------------------------------------------------------
# Observed graph
# ---------------------------------------------------------------------------

def get_observed_events(db, days: int = 7) -> dict:
    """Get observed event data from the DB for the last N days.

    Returns:
      event_counts   : event_type -> count
      policy_triggers: event_type -> list[{"policy": str, "count": int}]
    """
    rows = db.conn.execute(
        "SELECT event_type, COUNT(*) as cnt FROM events "
        "WHERE created_at >= datetime('now', ?) GROUP BY event_type ORDER BY cnt DESC",
        (f"-{days} days",),
    ).fetchall()
    event_counts = {r["event_type"]: r["cnt"] for r in rows}

    action_rows = db.conn.execute(
        "SELECT e.event_type, a.recipe, COUNT(*) as cnt "
        "FROM action_log a "
        "JOIN events e ON e.id = a.event_id "
        "WHERE e.created_at >= datetime('now', ?) "
        "GROUP BY e.event_type, a.recipe",
        (f"-{days} days",),
    ).fetchall()

    policy_triggers: dict = {}
    for r in action_rows:
        evt = r["event_type"]
        policy_triggers.setdefault(evt, []).append({
            "policy": r["recipe"],
            "count": r["cnt"],
        })

    return {
        "event_counts": event_counts,
        "policy_triggers": policy_triggers,
    }


# ---------------------------------------------------------------------------
# Graph comparison
# ---------------------------------------------------------------------------

def compare_graphs(static_graph: dict, observed: dict) -> dict:
    """Compare the static event graph with the observed event graph.

    Returns:
      in_static_only  : events declared in static graph but never observed
      in_observed_only: events observed in DB but not in static graph
      in_both         : events present in both
    """
    static_events = (
        set(static_graph["provided_by"].keys()) |
        set(static_graph["required_by"].keys())
    )
    observed_events = set(observed["event_counts"].keys())

    return {
        "in_static_only": sorted(static_events - observed_events),
        "in_observed_only": sorted(observed_events - static_events),
        "in_both": sorted(static_events & observed_events),
    }
