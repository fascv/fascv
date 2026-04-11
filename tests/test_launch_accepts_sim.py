import io
import queue
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from trading.ipc.events import Heartbeat
from trading.launch import _drain_heartbeat_queue, build_parser, main
from trading.run_gui import main as run_gui_main


class _FakeProcess:
    def __init__(self, name: str, alive: bool, pid: int, exitcode: int | None = None):
        self.name = name
        self._alive = alive
        self.pid = pid
        self.exitcode = exitcode

    def is_alive(self):
        return self._alive


class _EmptyHeartbeatQueue:
    def get(self, timeout=None):
        raise queue.Empty()


class _QueuedHeartbeatQueue:
    def __init__(self, items):
        self._items = list(items)

    def get(self, timeout=None):
        if self._items:
            return self._items.pop(0)
        raise queue.Empty()

    def get_nowait(self):
        return self.get(timeout=0)


class TestLaunchAcceptsSim(unittest.TestCase):
    def test_build_parser_accepts_sim(self):
        parser = build_parser()
        args = parser.parse_args(["--mode", "sim"])
        self.assertEqual(args.mode, "sim")

    def test_drain_heartbeat_queue_consumes_all_pending_heartbeats(self):
        now = time.time()
        fake_ctx = SimpleNamespace(
            q_heartbeat=_QueuedHeartbeatQueue(
                [
                    Heartbeat(ts=datetime.now(timezone.utc), process="md", seq=1),
                    Heartbeat(ts=datetime.now(timezone.utc), process="core", seq=2),
                    Heartbeat(ts=datetime.now(timezone.utc), process="exec", seq=3),
                ]
            )
        )
        last_hb = {"md": 0.0, "core": 0.0, "exec": 0.0}
        seen_heartbeat = {"md": False, "core": False, "exec": False}

        drained = _drain_heartbeat_queue(fake_ctx, last_hb, seen_heartbeat)

        self.assertEqual(drained, 3)
        self.assertTrue(all(seen_heartbeat.values()))
        self.assertGreaterEqual(last_hb["md"], now)
        self.assertGreaterEqual(last_hb["core"], now)
        self.assertGreaterEqual(last_hb["exec"], now)

    def test_main_sets_runtime_config_path_without_unboundlocalerror(self):
        parser = SimpleNamespace(
            add_argument=lambda *args, **kwargs: None,
            parse_args=lambda: SimpleNamespace(
                mode="sim",
                config="configs/example.yaml",
                env=".env",
                pidfile=None,
            ),
        )
        fake_ctx = SimpleNamespace(stop_event=SimpleNamespace(is_set=lambda: False))
        with patch("trading.launch.build_parser", return_value=parser), patch(
            "trading.launch.load_env"
        ), patch(
            "trading.launch.load_config",
            return_value=SimpleNamespace(raw={"control": {"enabled": False}, "impact": {"enabled": False}}),
        ), patch(
            "trading.launch._make_context",
            return_value=(fake_ctx, object()),
        ), patch(
            "trading.launch._start_processes",
            return_value=[],
        ), patch(
            "trading.launch._debug_watchdog_enabled",
            return_value=False,
        ), patch(
            "trading.launch.signal.signal",
        ), patch(
            "trading.launch._force_stop_processes",
        ):
            main()

    def test_main_preserves_existing_runtime_config_path(self):
        parser = SimpleNamespace(
            add_argument=lambda *args, **kwargs: None,
            parse_args=lambda: SimpleNamespace(
                mode="sim",
                config="configs/example.yaml",
                env=".env",
                pidfile=None,
            ),
        )
        fake_ctx = SimpleNamespace(stop_event=SimpleNamespace(is_set=lambda: False))
        seen_cfg = {}

        def _capture_context(cfg, mode):
            seen_cfg["cfg"] = cfg
            seen_cfg["mode"] = mode
            return fake_ctx, object()

        with patch("trading.launch.build_parser", return_value=parser), patch(
            "trading.launch.load_env"
        ), patch(
            "trading.launch.load_config",
            return_value=SimpleNamespace(
                raw={
                    "runtime": {"config_path": "/tmp/original-runtime.yaml"},
                    "control": {"enabled": False},
                    "impact": {"enabled": False},
                }
            ),
        ), patch(
            "trading.launch._make_context",
            side_effect=_capture_context,
        ), patch(
            "trading.launch._start_processes",
            return_value=[],
        ), patch(
            "trading.launch._debug_watchdog_enabled",
            return_value=False,
        ), patch(
            "trading.launch.signal.signal",
        ), patch(
            "trading.launch._force_stop_processes",
        ):
            main()

        self.assertEqual(seen_cfg["mode"], "sim")
        self.assertEqual(seen_cfg["cfg"]["runtime"]["config_path"], "/tmp/original-runtime.yaml")

    def test_main_journals_critical_child_exit_reason(self):
        parser = SimpleNamespace(
            add_argument=lambda *args, **kwargs: None,
            parse_args=lambda: SimpleNamespace(
                mode="sim",
                config="configs/example.yaml",
                env=".env",
                pidfile=None,
            ),
        )
        fake_ctx = SimpleNamespace(
            stop_event=threading.Event(),
            q_journal=queue.Queue(),
            q_control_core=queue.Queue(),
            q_control_exec=queue.Queue(),
            q_heartbeat=_EmptyHeartbeatQueue(),
        )
        fake_processes = [
            _FakeProcess("md", True, pid=101),
            _FakeProcess("core", False, pid=102, exitcode=17),
            _FakeProcess("exec", True, pid=103),
            _FakeProcess("journal", True, pid=104),
        ]
        stderr = io.StringIO()
        with patch("trading.launch.build_parser", return_value=parser), patch(
            "trading.launch.load_env"
        ), patch(
            "trading.launch.load_config",
            return_value=SimpleNamespace(raw={"control": {"enabled": False}, "impact": {"enabled": False}}),
        ), patch(
            "trading.launch._make_context",
            return_value=(fake_ctx, object()),
        ), patch(
            "trading.launch._start_processes",
            return_value=fake_processes,
        ), patch(
            "trading.launch._debug_watchdog_enabled",
            return_value=False,
        ), patch(
            "trading.launch.signal.signal",
        ), patch(
            "trading.launch._force_stop_processes",
        ), patch(
            "sys.stderr",
            stderr,
        ):
            main()

        journal_events = []
        while not fake_ctx.q_journal.empty():
            journal_events.append(fake_ctx.q_journal.get_nowait())

        watchdog_events = [evt for evt in journal_events if getattr(evt, "event_type", "") == "launch_watchdog"]
        self.assertTrue(watchdog_events)
        self.assertEqual(watchdog_events[0].payload["action"], "shutdown_lane")
        self.assertEqual(watchdog_events[0].payload["reason"], "critical_child_exit")
        self.assertEqual(watchdog_events[0].payload["child"], "core")
        self.assertEqual(watchdog_events[0].payload["exitcode"], 17)
        self.assertIn("action=shutdown_lane reason=critical_child_exit child=core exitcode=17", stderr.getvalue())

    def test_main_journals_heartbeat_stale_reason(self):
        parser = SimpleNamespace(
            add_argument=lambda *args, **kwargs: None,
            parse_args=lambda: SimpleNamespace(
                mode="sim",
                config="configs/example.yaml",
                env=".env",
                pidfile=None,
            ),
        )
        fake_ctx = SimpleNamespace(
            stop_event=threading.Event(),
            q_journal=queue.Queue(),
            q_control_core=queue.Queue(),
            q_control_exec=queue.Queue(),
            q_heartbeat=_EmptyHeartbeatQueue(),
        )
        fake_processes = [
            _FakeProcess("md", True, pid=201),
            _FakeProcess("core", True, pid=202),
            _FakeProcess("exec", True, pid=203),
            _FakeProcess("journal", True, pid=204),
        ]
        stderr = io.StringIO()
        with patch("trading.launch.build_parser", return_value=parser), patch(
            "trading.launch.load_env"
        ), patch(
            "trading.launch.load_config",
            return_value=SimpleNamespace(
                raw={
                    "control": {"enabled": False},
                    "impact": {"enabled": False},
                    "ipc": {
                        "heartbeat_timeout": -1.0,
                        "startup_grace_sec": 0.0,
                        "restart_critical": False,
                    },
                }
            ),
        ), patch(
            "trading.launch._make_context",
            return_value=(fake_ctx, object()),
        ), patch(
            "trading.launch._start_processes",
            return_value=fake_processes,
        ), patch(
            "trading.launch._debug_watchdog_enabled",
            return_value=False,
        ), patch(
            "trading.launch.signal.signal",
        ), patch(
            "trading.launch._force_stop_processes",
        ), patch(
            "sys.stderr",
            stderr,
        ):
            main()

        journal_events = []
        while not fake_ctx.q_journal.empty():
            journal_events.append(fake_ctx.q_journal.get_nowait())

        watchdog_events = [evt for evt in journal_events if getattr(evt, "event_type", "") == "launch_watchdog"]
        self.assertTrue(watchdog_events)
        self.assertEqual(watchdog_events[0].payload["action"], "shutdown_lane")
        self.assertEqual(watchdog_events[0].payload["reason"], "heartbeat_stale")
        self.assertIn(watchdog_events[0].payload["child"], {"md", "core", "exec"})
        self.assertIn("reason=heartbeat_stale", stderr.getvalue())

    def test_run_gui_embeds_original_config_path_in_temp_config(self):
        parser = SimpleNamespace(
            add_argument=lambda *args, **kwargs: None,
            parse_args=lambda: SimpleNamespace(
                mode="live",
                config="configs/example.yaml",
                env=".env",
                host=None,
                port=None,
            )
        )
        captured = {}
        fake_proc = SimpleNamespace(
            poll=lambda: 0,
            wait=lambda: 0,
            send_signal=lambda _sig: None,
        )
        temp_file = SimpleNamespace(name="/tmp/runtime-test.yaml")

        class _TempFile:
            name = temp_file.name

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, _text):
                return None

        def _capture_dump(data, handle, sort_keys=False):
            captured["cfg"] = data
            captured["name"] = handle.name

        with patch("trading.run_gui.argparse.ArgumentParser", return_value=parser), patch(
            "trading.run_gui.load_env"
        ), patch(
            "trading.run_gui.load_config",
            return_value=SimpleNamespace(raw={"control": {}, "runtime": {}}),
        ), patch(
            "trading.run_gui.tempfile.NamedTemporaryFile",
            return_value=_TempFile(),
        ), patch(
            "trading.run_gui.yaml.safe_dump",
            side_effect=_capture_dump,
        ), patch(
            "trading.run_gui.subprocess.Popen",
            return_value=fake_proc,
        ), patch(
            "trading.run_gui.signal.signal",
        ), patch(
            "trading.run_gui.os.unlink",
        ):
            run_gui_main()

        self.assertEqual(
            captured["cfg"]["runtime"]["config_path"],
            "/home/andi/Schreibtisch/codex/bitcoin2/configs/example.yaml",
        )
        self.assertEqual(captured["name"], "/tmp/runtime-test.yaml")


if __name__ == "__main__":
    unittest.main()
