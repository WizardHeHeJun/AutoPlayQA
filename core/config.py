from __future__ import annotations

import os
from typing import Any, Dict

import yaml


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        key = value[2:-1]
        return os.getenv(key, "")
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_config(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        # Everything has sane defaults; a missing config.yaml just means defaults.
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _resolve_env(data)
