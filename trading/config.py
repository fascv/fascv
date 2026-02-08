from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


@dataclass
class Config:
    raw: Dict[str, Any]


def load_config(path: str) -> Config:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load config files.")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("Config must be a mapping at top level.")
    return Config(raw=_expand_env(data))


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        key = obj[2:-1]
        return os.getenv(key, "")
    return obj
