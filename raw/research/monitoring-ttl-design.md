# Monitoring Policy TTL Design

## Investigation Findings (t-1)

Before designing a prevention mechanism, this iteration investigated the reported
"hex-ops-monitor stuck on q-153" problem:

- **No policy named `hex-ops-monitor` exists** in `policies/` or any subdirectory.
- **q-153 is fully completed** — all 11 tasks show ✓.
- **Zero q-153 events** in `events.db` (searched payload column).
- **`grep q-153` in daemon.log: no matches** — verify condition already satisfied.
- Active polling policies: only `devserver-monitor` (polls `timer.tick.5m` for
  dev7_2xlarge devserver availability — legitimate, by design).
- All other ops policies (`ops-completion-verify`, `ops-failure-pattern`,
  `ops-spec-digest`) are event-driven: they fire once per `boi.spec.completed` or
  `boi.spec.failed` event and cannot get "stuck" on a specific spec.

**Conclusion:** The immediate problem does not exist in the current system. The design
work that follows addresses the *class* of problem described in the spec context.

---

## Problem Statement

Any timer-triggered policy that monitors a transient resource (a BOI spec in
progress, a temporary process, a one-time file) can fire indefinitely after the
resource terminates. There is no built-in mechanism to say "stop when the target
is gone."

Current rate-limiting (`rate_limit: max_fires: N, window: Xh`) prevents bursting
but is a rolling window — it does not provide a hard lifetime ceiling.

The existing `max_fires` field on `Policy` (fires N times total, then auto-disables)
is the closest primitive, but it counts fires regardless of target state.

---

## Design Options

### Option A: `ttl` field — calendar duration ceiling

Add an optional `ttl` field at the **rule level** in YAML. After the rule has been
firing for longer than the TTL (measured from first fire), the daemon skips it and
logs a TTL-expired message.

```yaml
rules:
  - name: poll-availability
    trigger:
      event: timer.tick.5m
    ttl: 7d        # <-- new field
    actions:
      - type: shell
        command: bash ~/.hex-events/scripts/check-devserver.sh
```

**Implementation:** Store `first_fired_at` per (policy_name, rule_name) in
`policy_eval_log` (already exists). Before firing: compute
`now - first_fired_at > ttl_seconds`. If expired, skip + log.

**Pros:** Simple date math, no shell execution, zero knowledge of target needed,
backward compatible (absent = no TTL), universal.

**Cons:** Time-based, not state-aware. A BOI spec completing on day 1 still lets
the policy run until day 7. A devserver that stays down for 8 days gets silenced.

---

### Option B: `terminal_conditions` — shell-based liveness gate

Add an optional `terminal_conditions` list to the rule. Before firing, the daemon
evaluates each condition as a shell command. If any returns exit 0, the rule is
permanently disabled for this instance and logged.

```yaml
rules:
  - name: monitor-spec-q-153
    trigger:
      event: timer.tick.5m
    terminal_conditions:
      - command: "bash ~/.boi/boi spec q-153 2>/dev/null | grep -qE '(DONE|FAILED)'"
    actions:
      - type: shell
        command: bash ~/.hex-events/scripts/check-spec.sh q-153
```

**Implementation:** Evaluate each `terminal_conditions` command before running
actions. On exit 0, write a `disabled_at` timestamp to a persistent store
(e.g., a new `policy_disable_log` table or a JSON file in `~/.hex-events/state/`).
On subsequent evaluations, skip immediately.

**Pros:** State-aware, fires exactly until the target terminates, no time waste.

**Cons:** Requires target-specific knowledge in the policy YAML. Adds shell
execution overhead on every evaluation. More complex to implement.

---

### Option C: `monitor` action type — first-class monitoring primitive

Add a new `type: monitor` action that wraps liveness check + TTL + auto-disable
into one declarative block.

```yaml
actions:
  - type: monitor
    target_command: "bash ~/.boi/boi spec {{ event.queue_id }} 2>/dev/null"
    terminal_pattern: "(DONE|FAILED)"
    ttl: 7d
    on_terminal:
      - type: notify
        message: "Spec {{ event.queue_id }} reached terminal state, disabling monitor"
```

**Pros:** Self-contained, clearly expresses intent.
**Cons:** New action type requires changes to `actions/` module and all existing
monitoring policies need opt-in migration. Higher implementation cost.

---

### Option D: Self-audit "stuck monitor" detection (reactive)

Enhance the existing `self-audit` policy to detect policies that have fired N+
times with all-failures or no state change (i.e., every action returned the same
non-zero exit code for the last 24h).

```yaml
# In self-audit.yaml — add a rule:
- name: detect-stuck-monitors
  trigger:
    event: timer.tick.weekly
  actions:
    - type: shell
      command: python3 ~/.hex-events/scripts/detect-stuck-monitors.py
```

**Pros:** No policy changes. Works retroactively on existing policies.
**Cons:** Reactive, not preventive. Stuck monitors still fire until audit runs.
Alert-only unless we add auto-disable logic.

---

## Chosen Approach: Option A (`ttl`) with Option D as safety net

**Primary:** Implement `ttl` at the rule level. It is:
1. The simplest possible addition (date math, ~20 lines of code)
2. Backward compatible — existing policies are unchanged
3. Opt-in: add `ttl: 7d` to any rule that should not run forever
4. Universal: works for devserver polls, spec monitors, any timer-based rule
5. Auditable: expiry is logged, searchable in daemon.log

**Secondary (complementary, not blocking):** Extend self-audit to detect rules with
high fire counts and all-failure action logs. This catches cases where operators
forget to set `ttl` on a new polling rule.

---

## Implementation Spec for t-3

### 1. Schema (policy.py)

Add `ttl` to the `Rule` dataclass:
```python
@dataclass
class Rule:
    name: str
    trigger_event: str
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    ttl: Optional[str] = None   # e.g. "7d", "24h", "30m"
```

Parse from YAML in `_parse_rule()`:
```python
return Rule(
    name=name, trigger_event=trigger_event,
    conditions=conditions, actions=actions,
    ttl=data.get("ttl"),
)
```

### 2. TTL Check (hex_eventd.py)

In the event processing loop, before running a rule's actions, check TTL:

```python
def _check_rule_ttl(rule: Rule, policy_name: str, db_conn) -> bool:
    """Return True if rule is within TTL (or has no TTL). False = expired."""
    if not rule.ttl:
        return True
    ttl_secs = parse_duration(rule.ttl)
    row = db_conn.execute(
        """SELECT MIN(evaluated_at) FROM policy_eval_log
           WHERE policy_name = ? AND rule_name = ? AND action_taken = 1""",
        (policy_name, rule.name)
    ).fetchone()
    if not row or not row[0]:
        return True  # never fired, TTL clock hasn't started
    from datetime import datetime, timezone
    first_fire = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
    age_secs = (datetime.now(timezone.utc) - first_fire).total_seconds()
    if age_secs > ttl_secs:
        log.info(
            "TTL expired: policy=%s rule=%s ttl=%s age=%.0fs — skipping",
            policy_name, rule.name, rule.ttl, age_secs
        )
        return False
    return True
```

Call before action execution:
```python
if rule.ttl and not _check_rule_ttl(rule, policy.name, conn):
    # record in eval log as ttl_expired
    continue
```

### 3. YAML Usage (devserver-monitor example)

```yaml
rules:
  - name: poll-availability
    trigger:
      event: timer.tick.5m
    ttl: 7d    # stop polling after 7 days
    actions:
      - type: shell
        command: bash ~/.hex-events/scripts/check-devserver.sh
```

### 4. Logging

Log line format (searchable):
```
TTL expired: policy=devserver-monitor rule=poll-availability ttl=7d age=604800s — skipping
```

---

## Verification Criteria (for t-3)

The TTL field should be added to `devserver-monitor.yaml` with `ttl: 7d`. To
demonstrate it would have stopped a q-153 monitor:

Assume a hypothetical `hex-ops-monitor` policy was polling q-153 starting on the
day q-153 was first dispatched. q-153.iteration-1.json has a timestamp; q-153
completed within a few days. With `ttl: 7d`, the policy would have self-expired
within 7 days of first fire, well before the current date (2026-03-27).
