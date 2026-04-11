from __future__ import annotations

from typing import Any, Dict

from trading.config import load_config


def deep_merge(base: Any, overlay: Any) -> Any:
    """
    Merge overlay into base (recursively for dicts). Lists/scalars are replaced.
    Returns a new object; does not mutate inputs.
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    out: Dict[str, Any] = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_yaml_overlay(cfg: Dict[str, Any], overlay_path: str) -> Dict[str, Any]:
    """
    Load overlay YAML and deep-merge into cfg. Raises if overlay file is missing/invalid.
    """
    overlay = load_config(str(overlay_path)).raw
    return deep_merge(cfg, overlay)

