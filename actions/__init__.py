"""Action plugin registry for hex-events.

All action handler classes must implement:
    run(self, params: dict, event_payload: dict, db=None) -> dict

The `db` kwarg is an optional EventsDB instance passed by the daemon.
It is required by EmitAction for event chaining; other handlers may ignore it.
"""

_REGISTRY: dict[str, type] = {}

def register(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls
    return decorator

def get_action_handler(name: str):
    cls = _REGISTRY.get(name)
    if cls:
        return cls()
    return None

# Import plugins to trigger registration
from actions import shell, emit, update_file, notify, dagu
