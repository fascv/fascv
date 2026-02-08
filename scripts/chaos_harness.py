from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.request


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


def wait_for_exec_restart(pidfile: str, old_pid: int | None, timeout: float = 10.0) -> int | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            pidinfo = wait_for_pidfile(pidfile, timeout=1.0)
        except Exception:
            time.sleep(0.2)
            continue
        new_pid = pidinfo.get("children", {}).get("exec")
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(ROOT, "configs", "chaos.yaml"))
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--pidfile", default=os.path.join(ROOT, "logs", "chaos_pids.json"))
    parser.add_argument("--journal", default=os.path.join(ROOT, "logs", "chaos_journal.jsonl"))
    parser.add_argument("--artifacts", default=os.path.join(ROOT, "logs", "chaos_artifacts.log"))
    parser.add_argument("--status-url", default="http://127.0.0.1:8100/status")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.artifacts), exist_ok=True)
    if os.path.exists(args.artifacts):
        os.remove(args.artifacts)

    log(args.artifacts, "starting launch subprocess")
    proc = subprocess.Popen([
        "python3",
        "-m",
        "trading.launch",
        "--mode",
        args.mode,
        "--config",
        args.config,
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
            os.kill(md_pid, signal.SIGKILL)
            ok = wait_for_journal_event(args.journal, "core_stale", timeout=15.0)
            deadman = wait_for_deadman_disable(args.journal, timeout=15.0)
            log(args.artifacts, f"core_stale observed: {ok}")
            log(args.artifacts, f"deadman disabled observed: {deadman}")
            overall_ok = overall_ok and ok and deadman

        # 2) kill exec, verify pause and restart
        pidinfo = wait_for_pidfile(args.pidfile)
        exec_pid = pidinfo.get("children", {}).get("exec")
        if exec_pid:
            log(args.artifacts, "killing exec")
            os.kill(exec_pid, signal.SIGKILL)
            paused = wait_for_core_trading_enabled(args.status_url, False, timeout=15.0)
            restarted = wait_for_exec_restart(args.pidfile, exec_pid, timeout=15.0)
            resumed = wait_for_core_trading_enabled(args.status_url, True, timeout=15.0)
            log(args.artifacts, f"core paused observed: {paused}")
            log(args.artifacts, f"exec restarted: {restarted}")
            log(args.artifacts, f"core resumed observed: {resumed}")
            overall_ok = overall_ok and paused and (restarted is not None) and resumed

        status = read_status(args.status_url)
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

    log(args.artifacts, "PASS" if overall_ok else "FAIL")


if __name__ == "__main__":
    main()
