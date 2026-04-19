from __future__ import annotations

import os
import unittest
from unittest import mock

import scripts.rotation_status as rotation_status
import scripts.rotation_status_http as rotation_status_http


class TestRotationStatusHttp(unittest.TestCase):
    def test_default_port_uses_new_fallback(self) -> None:
        old = os.environ.pop("ROTATION_STATUS_HTTP_PORT", None)
        try:
            self.assertEqual(rotation_status_http._default_port(), 8960)
        finally:
            if old is not None:
                os.environ["ROTATION_STATUS_HTTP_PORT"] = old

    def test_default_port_allows_env_override(self) -> None:
        old = os.environ.get("ROTATION_STATUS_HTTP_PORT")
        os.environ["ROTATION_STATUS_HTTP_PORT"] = "8972"
        try:
            self.assertEqual(rotation_status_http._default_port(), 8972)
        finally:
            if old is None:
                os.environ.pop("ROTATION_STATUS_HTTP_PORT", None)
            else:
                os.environ["ROTATION_STATUS_HTTP_PORT"] = old

    def test_row_view_surfaces_fast_track_reason_and_lane(self) -> None:
        row = {
            "symbol": "INIT",
            "gate_reason": "no_trend_setup_lift_off",
            "selected_active": True,
            "selection_path": "fast_track",
            "selected_strategy": "breakout",
        }

        view = rotation_status_http._row_view(row)

        self.assertEqual(view["gate_reason"], "fast_track_breakout")
        self.assertEqual(view["lane"], "breakout")

    def test_render_status_shows_selected_lane_and_fast_track_reason(self) -> None:
        state = {
            "generated_at": "2026-03-14T03:50:16.762331+00:00",
            "selected": ["INIT"],
            "fraction": 0.25,
            "rows": [
                {
                    "symbol": "INIT",
                    "score": -9722.72,
                    "spread_bps": 11.4,
                    "ret15_bps": 80.6,
                    "rel15_bps": 89.0,
                    "corr_btc": 0.16,
                    "open_notional": 0.02,
                    "gate_reason": "no_trend_setup_lift_off",
                    "selected_active": True,
                    "selection_path": "fast_track",
                    "selected_strategy": "breakout",
                    "eligible": False,
                }
            ],
            "all_rows": [],
        }

        with mock.patch.object(rotation_status, "_load_state", return_value=state):
            rendered = rotation_status.render_status()

        self.assertIn("lane breakout", rendered)
        self.assertIn("gate fast_track_breakout", rendered)

    def test_build_view_model_fallback_running_lanes_do_not_become_selected(self) -> None:
        state = {
            "generated_at": "2026-03-17T06:00:00+00:00",
            "selected": [],
            "rows": [],
            "all_rows": [],
        }
        systemctl_out = "\n".join(
            [
                "codex-rotation-apt.service loaded active running",
                "codex-rotation-wld.service loaded active running",
            ]
        )

        with mock.patch.object(rotation_status_http.subprocess, "check_output", return_value=systemctl_out):
            view = rotation_status_http._build_view_model(state)

        self.assertEqual(view["selected"], [])
        self.assertEqual(view["active_rows"], [])
        self.assertEqual(view["next_rows"], [])
        self.assertEqual(
            [row["symbol"] for row in view["blocked_rows"]],
            ["APT", "WLD"],
        )
        self.assertTrue(all(row["gate_reason"] == "fallback_running_lane" for row in view["blocked_rows"]))

    def test_build_view_model_fallback_keeps_real_selected_lane(self) -> None:
        state = {
            "generated_at": "2026-03-17T06:00:00+00:00",
            "selected": ["APT"],
            "selected_strategy_map": {"APT": "rebound"},
            "rows": [],
            "all_rows": [],
        }
        systemctl_out = "\n".join(
            [
                "codex-rotation-apt.service loaded active running",
                "codex-rotation-wld.service loaded active running",
            ]
        )

        with mock.patch.object(rotation_status_http.subprocess, "check_output", return_value=systemctl_out):
            view = rotation_status_http._build_view_model(state)

        self.assertEqual(view["selected"], ["APT"])
        self.assertEqual([row["symbol"] for row in view["active_rows"]], ["APT"])
        self.assertEqual(view["active_rows"][0]["lane"], "rebound")
        self.assertEqual([row["symbol"] for row in view["blocked_rows"]], ["WLD"])

    def test_build_view_model_keeps_selected_scope_symbol_even_without_rows(self) -> None:
        state = {
            "generated_at": "2026-04-19T06:00:00+00:00",
            "selected": ["SIGN"],
            "watch_symbols": ["SIGN"],
            "selected_strategy_map": {"SIGN": "rebound"},
            "rows": [],
            "all_rows": [],
        }

        with mock.patch.object(rotation_status_http.subprocess, "check_output", return_value=""):
            view = rotation_status_http._build_view_model(state)

        self.assertEqual(view["selected"], ["SIGN"])
        self.assertEqual([row["symbol"] for row in view["active_rows"]], ["SIGN"])
        self.assertEqual(view["active_rows"][0]["lane"], "rebound")
        self.assertEqual(view["active_rows"][0]["gate_reason"], "fallback_scope_symbol")
        self.assertEqual(view["blocked_rows"], [])


if __name__ == "__main__":
    unittest.main()
