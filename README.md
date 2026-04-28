# ⚠️ ARCHIVED — Merged into hex-foundation (v0.8.0)

**This repo has been merged into [hex-foundation](https://github.com/mrap/hex) at `system/events/` as of v0.8.0 (2026-04-27).**

All future hex-events development happens in hex-foundation. This repo is kept for historical reference only.

## What moved where

| This repo | hex-foundation |
|-----------|---------------|
| `hex_eventd.py` | `system/events/hex_eventd.py` |
| `hex_emit.py` | `system/events/hex_emit.py` |
| `hex_events_cli.py` | `system/events/hex_events_cli.py` |
| `conditions.py` | `system/events/conditions.py` |
| `db.py` | `system/events/db.py` |
| `actions/` | `system/events/actions/` |
| `adapters/` | `system/events/adapters/` |
| `policies/` | `system/events/policies/` |
| `tests/` | `tests/events/` |
| `docs/` | `system/events/docs/` |

## Migration

If you're running hex-events standalone, upgrade to hex-foundation v0.8.0:
```bash
cd ~/hex && bash .hex/scripts/upgrade.sh
```

The installer deploys hex-events from `system/events/` — no external clone needed.

---

# ⚠️ ARCHIVED — Merged into hex-foundation

This repo has been merged into [hex-foundation](https://github.com/mrap/hex-foundation)
at `system/events/` as of v0.8.0 (2026-04-27).

All future development happens in hex-foundation. This repo is kept for history only.

---

# hex-events

Reactive event system for [hex](https://github.com/mrap/hex). Policy-driven automation — emit events, match conditions, fire actions.

## Quick Start

```bash
git clone https://github.com/mrap/hex-events ~/.hex-events
bash ~/.hex-events/install.sh
```

The installer creates a virtualenv, installs dependencies, and registers the daemon as a LaunchAgent (macOS).

## How It Works

```
emit event → events.db → daemon (2s poll) → policy match → conditions → action → log
```

Events land in a SQLite bus. The daemon polls every 2 seconds, evaluates YAML policies against incoming events, and fires actions. The daemon is a singleton (flock) and hot-reloads policies every 10 seconds — no restart needed when you add or edit a policy.

## Architecture

| Component | Purpose |
|-----------|---------|
| `hex_eventd.py` | Singleton daemon. Polls DB, matches policies, fires actions with retry and rate limiting. |
| `db.py` | SQLite event bus (WAL mode). Events, action log, deferred events, policy eval log. |
| `policy.py` | Policy/Rule/Condition/Action dataclasses. Loads YAML, supports glob event type matching. |
| `conditions.py` | Condition evaluator: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `glob`, `regex`, shell, count aggregation. |
| `actions/` | Plugin registry. Each action type implements `run()`. Includes: `shell`, `emit`, `notify`, `render`, `update-file`. |
| `hex_events_cli.py` | Debug/admin CLI: `status`, `history`, `inspect`, `trace`, `validate`, `graph`. |
| `hex_emit.py` | Event emission CLI. |
| `adapters/scheduler.py` | Cron-based timer events configured via `adapters/scheduler.yaml`. |
| `policy_validator.py` | Static schema validation for policy YAML. |

## Policy Format

Policies are YAML files in `policies/`. The daemon hot-reloads them every 10 seconds.

```yaml
name: example-policy
description: Notify when a job completes

rules:
  - name: on-completed
    trigger:
      event: "my.event.*"           # glob patterns supported
    conditions:
      - field: payload.status
        op: eq
        value: "completed"
    actions:
      - type: shell
        command: "echo 'Event fired: {{ event.payload.status }}'"
        timeout: 60
        retries: 3
        on_failure:
          - type: notify
            message: "Failed: {{ action.stderr }}"
```

### Condition Operators

| Operator | Description |
|----------|-------------|
| `eq`, `neq` | Equality / inequality |
| `gt`, `gte`, `lt`, `lte` | Numeric comparison |
| `contains` | Substring match |
| `glob` | Shell glob pattern |
| `regex` | Regular expression |
| `shell` | Exit 0 = pass. Supports Jinja2: `type: shell`, `command: "test -f /path"` |
| `count(event.type, 1h)` | Count events in a time window. Supports payload filter: `count(event.type, 1h, field=value)` |

All conditions in a rule are ANDed. Short-circuit evaluation applies.

### Field Resolution

- `field: status` — looks up `payload["status"]`
- `field: payload.nested.key` — traverses `payload["nested"]["key"]`

### Action Types

| Type | Key Params | Description |
|------|-----------|-------------|
| `shell` | `command`, `timeout` (60s), `retries` (3) | Run a shell command. Jinja2 templating with `{{ event.* }}`. |
| `emit` | `event`, `payload`, `delay`, `cancel_group` | Emit a new event (chain). `delay: 5m` defers it. |
| `notify` | `message` | Send a notification via `hex-notify.sh`. |
| `update-file` | (see source) | Update a file on disk. |
| `dagu` | (see source) | Trigger a Dagu workflow. |

All actions support `on_success` and `on_failure` sub-actions. Sub-actions can reference `{{ action.stdout }}`, `{{ action.stderr }}`, `{{ action.returncode }}`. Actions retry with exponential backoff.

### Optional Policy Fields

```yaml
rate_limit:
  max_fires: 3
  window: 1h
lifecycle: persistent          # persistent | oneshot-delete | oneshot-disable
max_fires: 10                  # auto-disable after N fires
provides:
  events: [output.event]       # documentation: events this policy emits
requires:
  events: [input.event]        # documentation: events this policy consumes
```

### Workflows (Grouped Policies)

Subdirectories in `policies/` are workflows with a shared `_config.yaml`:

```
policies/
  my-workflow/
    _config.yaml        # name, enabled, shared config
    step-one.yaml
    step-two.yaml
```

Disable a workflow by setting `enabled: false` in `_config.yaml` or adding a `.disabled` file.

## CLI Reference

```bash
# Emit an event
./hex_emit.py event.type '{"key": "value"}' source-name

# Debug and admin
./hex_events_cli.py status                        # Daemon status, policy count, queue depth
./hex_events_cli.py history [--since 1]           # Event timeline (hours)
./hex_events_cli.py inspect <event-id>            # Full trace for one event
./hex_events_cli.py trace <event-id>              # Policy evaluation trace
./hex_events_cli.py trace --policy <name> --since 1  # Policy trace over time
./hex_events_cli.py validate                      # Static policy graph validation
./hex_events_cli.py graph [--observed]            # Event dependency graph
./hex_events_cli.py recipes                       # List loaded policies
```

## Static verification (v0.2.0)

hex-events owns the event model and provides a compiler that statically verifies policies before they land in `~/.hex-events/policies/`. The compile step is the trust boundary — mistakes surface at `hex-integration install` time, not weeks later in telemetry.

### New CLI subcommands

```bash
# Emit the full {event → {producers[], consumers[]}} catalog as JSON
./hex_events_cli.py list-events [--format json]

# Run all validators — exit 0 = clean, 1 = errors, 2 = warnings only
./hex_events_cli.py check <bundle-dir-or-policy-file>
./hex_events_cli.py check <path> --format json
./hex_events_cli.py check <path> --permissive   # warn-only (legacy corpus)
./hex_events_cli.py check --all                 # scan entire ~/.hex-events/policies/

# Compile: runs check + writes output with manifest headers if clean
./hex_events_cli.py compile <bundle-dir>
./hex_events_cli.py compile <bundle-dir> --dry-run
```

### What the compiler checks

| Check | Validator | Error code |
|-------|-----------|------------|
| Unknown event subscription (no producer) | producer-check | `EVENT_NO_PRODUCER` |
| Wrong schema (flat `trigger:/action:`) | schema | `SCHEMA_FLAT_FORM` |
| Duplicate rule name within a policy | dead-code | `DUPLICATE_RULE_NAME` |
| Duplicate policy name across corpus | dead-code | `DUPLICATE_POLICY_NAME` |
| Unknown action type | dead-code | `UNKNOWN_ACTION_TYPE` |
| Rule with no actions | dead-code | `NO_ACTIONS` |
| Rate-limit window smaller than trigger cadence | dead-code | `RATE_LIMIT_CADENCE_MISMATCH` (warning) |

### Compiled policy headers

Every policy compiled through `hex-events compile` carries:

```yaml
# generated_from: integrations/<bundle>/events/<stem>.yaml
# generated_at: <ISO-8601>
# compiler_version: 1.0.0
# checks_passed: schema, producer-check, dead-code
```

`hex-integration install` shells out to `hex-events compile` for every `events/*.yaml` in a bundle. On compile error, install exits non-zero and no files land in `~/.hex-events/policies/`.

## Emitting Events

```bash
# From shell
./hex_emit.py boi.spec.completed '{"spec_id": "abc"}' hex:boi

# From a policy action (event chaining)
actions:
  - type: emit
    event: next.event.type
    payload:
      key: "{{ event.some_field }}"
    delay: 5m              # optional: defer by duration
    cancel_group: my-group # optional: cancels pending deferred events with same group
```

### Timer Events (Cron)

Configure in `adapters/scheduler.yaml`:

```yaml
schedules:
  - name: 30m-tick
    cron: "*/30 * * * *"
    event: timer.tick.30m
```

## Adding a Policy

1. Create a YAML file in `policies/`
2. At minimum: `name`, `rules` with `trigger` and `actions`
3. Validate: `./hex_events_cli.py validate`
4. The daemon picks it up within 10 seconds

## Adding an Action Type

1. Create `actions/my_action.py`, implement a class with `run(self, params, event_payload, db=None, workflow_context=None) -> dict`
2. Decorate with `@register("my-action-type")`
3. Import it in `actions/__init__.py`
4. Add the type to `VALID_ACTION_TYPES` in `policy_validator.py`

## Database

SQLite at `events.db` (WAL mode). Tables:

- `events` — event bus (event_type, payload JSON, source, processed_at, dedup_key)
- `action_log` — action fire history per event
- `deferred_events` — delayed events waiting to fire
- `policy_eval_log` — full evaluation trace (policies matched, conditions passed/failed, rate limited)

Events older than 7 days are auto-deleted by the janitor.

## Requirements

- Python 3.10+
- pyyaml >= 6.0
- croniter >= 1.3
- jinja2 >= 3.1

## Tests

```bash
make test
# or
./venv/bin/python3 -m pytest tests/ -v --tb=short
```

## License

MIT — see [LICENSE](LICENSE).
