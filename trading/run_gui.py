from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict

import yaml

from trading.config import load_config
from trading.utils.env import load_env


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def main() -> None:
    parser = argparse.ArgumentParser(description="Start engine with built-in web GUI")
    parser.add_argument("--mode", choices=["paper", "live", "sim"], default="paper")
    parser.add_argument("--config", default="configs/paper.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    load_env(args.env)
    cfg = load_config(args.config).raw
    cfg.setdefault("control", {})
    cfg["control"]["enabled"] = True
    cfg.setdefault("runtime", {})
    cfg["runtime"]["config_path"] = os.path.abspath(args.config)
    if args.host is not None:
        cfg["control"]["host"] = args.host
    if args.port is not None:
        cfg["control"]["port"] = args.port

    host = _cfg(cfg, "control.host", "127.0.0.1")
    port = int(_cfg(cfg, "control.port", 8000))

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
        yaml.safe_dump(cfg, tf, sort_keys=False)
        tmp_cfg = tf.name

    cmd = [
        sys.executable,
        "-m",
        "trading.launch",
        "--mode",
        args.mode,
        "--config",
        tmp_cfg,
        "--env",
        args.env,
    ]

    print(f"GUI: http://{host}:{port}/")
    print(f"API docs: http://{host}:{port}/docs")

    proc = subprocess.Popen(cmd)

    def _forward(sig, _frame):
        if proc.poll() is None:
            proc.send_signal(sig)

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)

    try:
        proc.wait()
    finally:
        try:
            os.unlink(tmp_cfg)
        except Exception:
            pass


if __name__ == "__main__":
    main()
