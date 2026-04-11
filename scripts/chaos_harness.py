from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

import yaml


ROOT = os.path.dirname(os.path.dirname(__file__))


def log(path: str, msg: str) -> None:
    print(msg)
    with open(path, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def read_status(status_url: str):
    with urllib.request.urlopen(status_url, timeout=2) as resp:
        return json.loads(resp.read().decode())


def wait_for_pidfile(pidfile: str, timeout: float = 10.0) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(pidfile):
            with open(pidfile, "r", encoding="utf-8") as f:
                return json.load(f)
        time.sleep(0.2)
    raise RuntimeError("pidfile not found")


def wait_for_journal_event(journal: str, event_type: str, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(journal):
            with open(journal, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if payload.get("event_type") == event_type:
                        return True
        time.sleep(0.2)
    return False


def wait_for_deadman_disable(journal: str, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(journal):
            with open(journal, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if payload.get("event_type") == "deadman":
                        info = payload.get("payload", {})
                        if info.get("timeout") == 0:
                            return True
        time.sleep(0.2)
    return False


def wait_for_child_restart(pidfile: str, child: str, old_pid: int | None, timeout: float = 10.0) -> int | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            pidinfo = wait_for_pidfile(pidfile, timeout=1.0)
        except Exception:
            time.sleep(0.2)
            continue
        new_pid = pidinfo.get("children", {}).get(child)
        if new_pid and new_pid != old_pid:
            return new_pid
        time.sleep(0.2)
    return None


def wait_for_core_trading_enabled(status_url: str, expected: bool, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            status = read_status(status_url)
        except Exception:
            time.sleep(0.2)
            continue
        core_state = status.get("data", {}).get("core", {})
        if core_state.get("trading_enabled") == expected:
            return True
        time.sleep(0.2)
    return False


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, int(port)))
            return True
        except OSError:
            return False


def _find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _runtime_config_with_control_port(config_path: str, control_host: str, desired_port: int) -> tuple[str, int]:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise RuntimeError("config root must be mapping")
    control = cfg.get("control")
    if not isinstance(control, dict):
        control = {}
        cfg["control"] = control
    configured_port = int(control.get("port", desired_port))
    port = desired_port
    if not _port_available(control_host, desired_port):
        port = _find_free_port(control_host)
    control["host"] = control_host
    control["port"] = int(port)
    if configured_port != port:
        print(f"control.port {configured_port} belegt, nutze freien Port {port}")
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="chaos_runtime_",
        suffix=".yaml",
        delete=False,
        dir=os.path.join(ROOT, "logs"),
    )
    with tmp:
        yaml.safe_dump(cfg, tmp, sort_keys=False)
    return tmp.name, int(port)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(ROOT, "configs", "chaos.yaml"))
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--pidfile", default=os.path.join(ROOT, "logs", "chaos_pids.json"))
    parser.add_argument("--journal", default=os.path.join(ROOT, "logs", "chaos_journal.jsonl"))
    parser.add_argument("--artifacts", default=os.path.join(ROOT, "logs", "chaos_artifacts.log"))
    parser.add_argument("--status-url", default=None)
    parser.add_argument("--control-host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8100)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.artifacts), exist_ok=True)
    if os.path.exists(args.artifacts):
        os.remove(args.artifacts)
    if os.path.exists(args.pidfile):
        os.remove(args.pidfile)
    if os.path.exists(args.journal):
        os.remove(args.journal)

    runtime_config = None
    try:
        runtime_config, runtime_port = _runtime_config_with_control_port(
            config_path=args.config,
            control_host=args.control_host,
            desired_port=int(args.control_port),
        )
    except Exception as exc:
        raise RuntimeError(f"failed to prepare runtime config: {exc}") from exc

    status_url = args.status_url or f"http://{args.control_host}:{runtime_port}/status"
    log(args.artifacts, f"status_url: {status_url}")
    log(args.artifacts, "starting launch subprocess")
    proc = subprocess.Popen([
        "python3",
        "-m",
        "trading.launch",
        "--mode",
        args.mode,
        "--config",
        runtime_config,
        "--pidfile",
        args.pidfile,
    ])

    overall_ok = True
    try:
        pidinfo = wait_for_pidfile(args.pidfile)
        log(args.artifacts, f"pidfile: {pidinfo}")

        # 1) kill md (simulates hard ws disconnect)
        md_pid = pidinfo.get("children", {}).get("md")
        if md_pid:
            log(args.artifacts, "killing md")
            try:
                os.kill(md_pid, signal.SIGKILL)
            except ProcessLookupError:
                log(args.artifacts, f"md process already gone: {md_pid}")
                overall_ok = False
            ok = wait_for_journal_event(args.journal, "core_stale", timeout=15.0)
            md_restarted = wait_for_child_restart(args.pidfile, "md", md_pid, timeout=15.0)
            deadman = wait_for_deadman_disable(args.journal, timeout=15.0)
            log(args.artifacts, f"core_stale observed: {ok}")
            log(args.artifacts, f"md restarted: {md_restarted}")
            log(args.artifacts, f"deadman disabled observed: {deadman}")
            overall_ok = overall_ok and deadman and (ok or (md_restarted is not None))

        # 2) kill exec, verify pause and restart
        pidinfo = wait_for_pidfile(args.pidfile)
        exec_pid = pidinfo.get("children", {}).get("exec")
        if exec_pid:
            log(args.artifacts, "killing exec")
            try:
                os.kill(exec_pid, signal.SIGKILL)
            except ProcessLookupError:
                log(args.artifacts, f"exec process already gone: {exec_pid}")
                overall_ok = False
            paused = wait_for_core_trading_enabled(status_url, False, timeout=15.0)
            restarted = wait_for_child_restart(args.pidfile, "exec", exec_pid, timeout=15.0)
            resumed = wait_for_core_trading_enabled(status_url, True, timeout=15.0)
            log(args.artifacts, f"core paused observed: {paused}")
            log(args.artifacts, f"exec restarted: {restarted}")
            log(args.artifacts, f"core resumed observed: {resumed}")
            overall_ok = overall_ok and (restarted is not None) and resumed

        status = read_status(status_url)
        log(args.artifacts, f"final status: {status}")
    except Exception as exc:
        overall_ok = False
        log(args.artifacts, f"chaos harness exception: {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except Exception:
            proc.kill()
        if runtime_config and os.path.exists(runtime_config):
            os.remove(runtime_config)

    log(args.artifacts, "PASS" if overall_ok else "FAIL")


if __name__ == "__main__":
    main()
