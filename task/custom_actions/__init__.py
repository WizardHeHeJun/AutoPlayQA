"""In-process registry for deterministic custom task actions.

A custom action fills the gap between a single executor primitive
(click/drag/...) and a full agent handoff: a complex but fully deterministic
step implemented in Python, e.g. "swipe the list until a text shows up".
Handlers run inside the engine process - no LLM, no subprocess.

Handler signature:
    handler(ctx: CustomActionContext, params: Dict) -> List[Dict]

The returned list uses executor result format ({"ok": "True", ...}); the
engine fails the node on the first entry whose ok != "True". Raising is
equivalent to failing the node with the exception message. An empty list
means success.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

Handler = Callable[["CustomActionContext", Dict], List[Dict]]


@dataclass
class CustomActionContext:
    """Engine capabilities exposed to a handler (eyes + hands, no brain)."""

    device_id: str
    executor: Any  # ActionExecutor: execute(device_id, action_dict, tracer)
    hub: Any  # RecognizerHub: recognize(device_id, recognition_spec) -> hit | None
    hit: Dict  # recognition hit of the node that triggered this action
    logger: Any
    tracer: Any = None


_REGISTRY: Dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    if not isinstance(name, str) or not name:
        raise ValueError("Custom action name must be a non-empty string")

    def decorator(fn: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"Custom action '{name}' already registered")
        _REGISTRY[name] = fn
        return fn

    return decorator


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_handler(name: str) -> Optional[Handler]:
    return _REGISTRY.get(name)


def registered_names() -> Tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _import_handler_modules() -> None:
    """Import every handler module in this package so their @register runs.

    Auto-discovery instead of a hand-maintained import list: dropping
    `task/custom_actions/<name>.py` into the package is all it takes for its
    handlers to be registered — no wiring edit here that a new module can
    forget. Modules are imported in name order so the registry is
    deterministic, and `_`-prefixed files are skipped as private helpers.

    A module that fails to import raises out of this package import instead of
    being skipped: a silently missing handler would only surface much later,
    mid-run, as "Unregistered custom action" on a device.
    """
    for module in sorted(info.name for info in pkgutil.iter_modules(__path__)):
        if module.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module}")


# Kept last so handler modules can import `register` from this
# partially initialized module.
_import_handler_modules()
