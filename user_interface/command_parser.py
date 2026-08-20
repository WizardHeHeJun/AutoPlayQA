from __future__ import annotations

from typing import Dict


KNOWN_COMMANDS = {"help", "exit", "device", "agent", "click", "drag", "input", "action", "debug", "task", "record"}
COMMAND_ALIASES = {
    "devices": "device",
    "agents": "agent",
}


def _parse_name_and_days(parts, result_type: str) -> Dict:
    """Parse `task <sub> [name] [--days N]` (order-insensitive after the sub).

    Shared by `task health` and `task handoffs`: both are cross-run read-only
    reports filtered the same way (one optional task name, one optional window).
    """
    task_name = None
    days = None
    rest = parts[2:]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token.lower() == "--days":
            if i + 1 >= len(rest):
                return {"type": "unknown", "raw": " ".join(parts)}
            try:
                days = int(rest[i + 1])
            except ValueError:
                return {"type": "unknown", "raw": " ".join(parts)}
            i += 2
        else:
            if task_name is not None:
                return {"type": "unknown", "raw": " ".join(parts)}
            task_name = token
            i += 1
    return {"type": result_type, "task_name": task_name, "days": days}


def parse_command(raw: str) -> Dict:
    raw = raw.strip()
    if not raw:
        return {"type": "empty"}

    parts = raw.split()
    parts_lower = [p.lower() for p in parts]
    cmd = COMMAND_ALIASES.get(parts_lower[0], parts_lower[0])

    if cmd == "help":
        return {"type": "help"}
    if cmd == "exit":
        return {"type": "exit"}
    if cmd == "debug":
        sub = parts_lower[1] if len(parts) > 1 else ""
        if sub in ("on", "off", "status"):
            return {"type": "debug_control", "sub": sub}
        return {"type": "unknown", "raw": raw}
    if cmd == "task":
        sub = parts_lower[1] if len(parts) > 1 else ""
        if sub == "list":
            return {"type": "task_list"}
        if sub in ("run", "show", "save", "renumber") and len(parts) > 2:
            return {"type": f"task_{sub}", "name": parts[2]}
        if sub == "suites":
            return {"type": "task_suite_list"}
        if sub == "suite" and len(parts) > 2:
            return {
                "type": "task_suite",
                "name": parts[2],
                "device_id": parts[3] if len(parts) > 3 else None,
            }
        if sub == "resume" and len(parts) > 3:
            return {"type": "task_resume", "name": parts[2], "node": parts[3]}
        if sub == "cache":
            action = parts_lower[2] if len(parts) > 2 else "status"
            if action in ("clear", "status"):
                return {"type": "task_cache", "sub": action}
        if sub == "lint" and len(parts) > 2:
            return {"type": "task_lint", "name": parts[2]}
        if sub == "health":
            return _parse_name_and_days(parts, "task_health")
        if sub == "handoffs":
            return _parse_name_and_days(parts, "task_handoffs")
        return {"type": "unknown", "raw": raw}
    if cmd == "record":
        sub = parts_lower[1] if len(parts) > 1 else ""
        if sub == "gestures":
            action = parts_lower[2] if len(parts) > 2 else ""
            if action in ("start", "stop", "status"):
                return {
                    "type": "record_gestures",
                    "sub": action,
                    "device_id": parts[3] if len(parts) > 3 else None,
                }
            return {"type": "unknown", "raw": raw}
        if sub in ("on", "off", "status"):
            return {"type": "record_control", "sub": sub}
        return {"type": "unknown", "raw": raw}
    if cmd == "device" and len(parts) > 1 and parts_lower[1] == "list":
        return {"type": "device_list"}
    if cmd == "device" and len(parts) > 2 and parts_lower[1] == "connect":
        return {"type": "device_connect", "address": parts[2]}
    if cmd == "device" and len(parts) > 1 and parts_lower[1] == "disconnect":
        return {"type": "device_disconnect", "address": parts[2] if len(parts) > 2 else None}
    if cmd == "device" and len(parts) > 2 and parts_lower[1] == "tcpip":
        try:
            port = int(parts[3]) if len(parts) > 3 else 5555
        except ValueError:
            return {"type": "unknown", "raw": raw}
        return {"type": "device_tcpip", "device_id": parts[2], "port": port}
    if cmd == "device" and len(parts) > 3 and parts_lower[1] == "pair":
        return {"type": "device_pair", "address": parts[2], "code": parts[3]}
    if cmd == "agent" and len(parts) > 1 and parts_lower[1] == "list":
        return {"type": "agent_list"}
    if cmd == "agent" and len(parts) > 2 and parts_lower[1] == "select":
        return {"type": "agent_select", "selector": parts[2]}
    if cmd == "click" and len(parts) == 3:
        try:
            return {
                "type": "actions",
                "actions": [{"type": "click", "params": {"x": int(parts[1]), "y": int(parts[2])}}],
            }
        except ValueError:
            return {"type": "unknown", "raw": raw}
    if cmd == "drag" and len(parts) in (5, 6):
        try:
            duration = int(parts[5]) if len(parts) == 6 else 500
            return {
                "type": "actions",
                "actions": [
                    {
                        "type": "drag",
                        "params": {
                            "x1": int(parts[1]),
                            "y1": int(parts[2]),
                            "x2": int(parts[3]),
                            "y2": int(parts[4]),
                            "duration_ms": duration,
                        },
                    }
                ],
            }
        except ValueError:
            return {"type": "unknown", "raw": raw}
    if cmd == "input" and len(parts) > 1:
        return {
            "type": "actions",
            "actions": [{"type": "input_text", "params": {"text": raw[len("input ") :].strip()}}],
        }
    if cmd == "action" and len(parts) > 1:
        return {"type": "text_action", "text": raw[len("action ") :].strip()}

    if cmd in KNOWN_COMMANDS:
        return {"type": "unknown", "raw": raw}

    # Default fallback: treat free-form input as natural-language action.
    return {"type": "text_action", "text": raw}
