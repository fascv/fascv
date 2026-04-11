from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from scripts import rotation_apply_active_lanes
from scripts.rotation_auto_coin_selector import _apply_signature
from trading.rotation_strategy_runtime import (
    ROTATION_RUNTIME_CONFIG_VERSION,
    build_rotation_runtime_config,
    build_selected_alpha_map,
    rotation_strategy_to_alpha_type,
)


class TestRotationStrategyRuntime(unittest.TestCase):
    def test_rotation_strategy_to_alpha_type_maps_meta_families(self) -> None:
        self.assertEqual(rotation_strategy_to_alpha_type("staircase"), "continuation")
        self.assertEqual(rotation_strategy_to_alpha_type("pullback_continuation"), "continuation")
        self.assertEqual(rotation_strategy_to_alpha_type("breakout_retest"), "breakout")
        self.assertEqual(rotation_strategy_to_alpha_type("relative_strength"), "trend")
        self.assertEqual(rotation_strategy_to_alpha_type("rebound"), "swing")
        self.assertEqual(rotation_strategy_to_alpha_type("momentum"), "trend")

    def test_build_rotation_runtime_config_sets_runtime_metadata(self) -> None:
        base_cfg = {
            "alpha": {
                "type": "continuation",
                "continuation": {"trend_min_bps": 6.0},
                "trend": {"threshold_bps": 4.0},
            },
        }
        runtime_cfg, alpha_type = build_rotation_runtime_config(base_cfg, "relative_strength")
        self.assertEqual(alpha_type, "trend")
        self.assertEqual(runtime_cfg["alpha"]["type"], "trend")
        self.assertEqual(runtime_cfg["runtime"]["rotation_strategy_name"], "relative_strength")
        self.assertEqual(runtime_cfg["runtime"]["rotation_alpha_type"], "trend")
        self.assertEqual(
            runtime_cfg["runtime"]["rotation_runtime_config_version"],
            ROTATION_RUNTIME_CONFIG_VERSION,
        )
        self.assertEqual(base_cfg["alpha"]["type"], "continuation")

    def test_build_rotation_runtime_config_applies_staircase_overlay(self) -> None:
        base_cfg = {
            "alpha": {
                "type": "continuation",
                "continuation": {
                    "rebound_trigger_bps": 4.0,
                    "max_structure_range_pos": 0.97,
                    "staircase_min_drawdown_from_peak_bps": 6.0,
                    "staircase_max_context_range_pos": 0.94,
                },
            },
        }
        runtime_cfg, alpha_type = build_rotation_runtime_config(base_cfg, "staircase")
        self.assertEqual(alpha_type, "continuation")
        cont = runtime_cfg["alpha"]["continuation"]
        self.assertEqual(cont["rebound_trigger_bps"], 0.0)
        self.assertEqual(cont["max_structure_range_pos"], 1.0)
        self.assertEqual(cont["staircase_min_drawdown_from_peak_bps"], 0.0)
        self.assertEqual(cont["staircase_max_context_range_pos"], 0.99)
        base_cont = base_cfg["alpha"]["continuation"]
        self.assertEqual(base_cont["rebound_trigger_bps"], 4.0)
        self.assertEqual(base_cont["max_structure_range_pos"], 0.97)

    def test_build_rotation_runtime_config_sanitizes_invalid_swing_bands(self) -> None:
        base_cfg = {
            "alpha": {
                "type": "swing",
                "swing": {
                    "buy_band": 0.55,
                    "sell_band": 0.50,
                },
            }
        }
        runtime_cfg, alpha_type = build_rotation_runtime_config(base_cfg, "rebound")

        self.assertEqual(alpha_type, "swing")
        self.assertLess(runtime_cfg["alpha"]["swing"]["buy_band"], runtime_cfg["alpha"]["swing"]["sell_band"])
        self.assertEqual(base_cfg["alpha"]["swing"]["buy_band"], 0.55)
        self.assertEqual(base_cfg["alpha"]["swing"]["sell_band"], 0.50)

    def test_apply_signature_tracks_runtime_config_version_alpha_and_strategy_map(self) -> None:
        old_payload = {
            "selected": ["ADA", "TIA"],
            "watch_symbols": ["ADA", "TIA", "INIT"],
            "fraction": 0.5,
            "profile": "scalp_uptrend",
            "selected_fraction_map": {"ADA": 0.5, "TIA": 0.5},
            "profile_values": {"entry_edge_bps": 0.7},
        }
        new_payload = dict(old_payload)
        new_payload["runtime_config_version"] = ROTATION_RUNTIME_CONFIG_VERSION
        new_payload["selected_strategy_map"] = {
            "ADA": "staircase",
            "TIA": "continuation",
        }
        new_payload["selected_alpha_map"] = build_selected_alpha_map(
            {
                "ADA": "breakout_retest",
                "TIA": "relative_strength",
            }
        )
        self.assertNotEqual(_apply_signature(old_payload), _apply_signature(new_payload))

    def test_write_lane_runtime_config_preserves_env_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "base.yaml"
            runtime_path = Path(tmpdir) / "runtime.yaml"
            base_path.write_text(
                "\n".join(
                    [
                        "live:",
                        "  api_key: ${BINANCE_API_KEY}",
                        "  api_secret: ${BINANCE_API_SECRET}",
                        "alpha:",
                        "  type: continuation",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(rotation_apply_active_lanes, "_lane_base_config_path", return_value=base_path),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_runtime_config_path",
                    return_value=runtime_path,
                ),
            ):
                alpha_type, changed = rotation_apply_active_lanes._write_lane_runtime_config(
                    "NEAR",
                    "relative_strength",
                    "selected_strategy_map",
                )
            self.assertEqual(alpha_type, "trend")
            self.assertTrue(changed)
            runtime_cfg = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(runtime_cfg["live"]["api_key"], "${BINANCE_API_KEY}")
            self.assertEqual(runtime_cfg["live"]["api_secret"], "${BINANCE_API_SECRET}")
            self.assertEqual(runtime_cfg["alpha"]["type"], "trend")

    def test_ensure_lane_base_config_repairs_invalid_config_from_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            broken_xrp = tmp_path / "live_binance_xrp_usdc_rotation.yaml"
            template_xlm = tmp_path / "live_binance_xlm_usdc_rotation.yaml"
            broken_xrp.write_text("\n", encoding="utf-8")
            template_xlm.write_text(
                yaml.safe_dump(
                    {
                        "md": {"pair": "XLM/USDC"},
                        "exec": {"pair": "XLM/USDC"},
                        "impact": {
                            "symbol": "XLM/USDC",
                            "api_url": (
                                "http://127.0.0.1:8055/signal/multi-asset-probability"
                                "?target=XLM&lookback=72h"
                            ),
                        },
                        "control": {"port": 8054},
                        "live": {"symbol": "XLM/USDC"},
                        "journal": {
                            "db_path": "logs/journal_live_binance_xlm_usdc_rotation.db",
                            "json_path": "logs/journal_live_binance_xlm_usdc_rotation.jsonl",
                        },
                        "alpha": {"type": "continuation"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            def fake_base_path(symbol: str) -> Path:
                if symbol == "XRP":
                    return broken_xrp
                if symbol == "XLM":
                    return template_xlm
                raise AssertionError(symbol)

            fake_lanes = {
                "XRP": {
                    "slug": "xrp",
                    "ports": (8024, 9210, 9220, 9230, 9240),
                    "config": "configs/live_binance_xrp_usdc_rotation.yaml",
                },
                "XLM": {
                    "slug": "xlm",
                    "ports": (8054, 8950, 8960, 8970, 8980),
                    "config": "configs/live_binance_xlm_usdc_rotation.yaml",
                },
            }

            with (
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(rotation_apply_active_lanes, "FALLBACK_TEMPLATE_SYMBOL", "XLM"),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_base_config_path",
                    side_effect=fake_base_path,
                ),
            ):
                repaired_path = rotation_apply_active_lanes._ensure_lane_base_config("XRP")

            self.assertEqual(repaired_path, broken_xrp)
            repaired_cfg = yaml.safe_load(broken_xrp.read_text(encoding="utf-8"))
            self.assertEqual(repaired_cfg["md"]["pair"], "XRP/USDC")
            self.assertEqual(repaired_cfg["exec"]["pair"], "XRP/USDC")
            self.assertEqual(repaired_cfg["impact"]["symbol"], "XRP/USDC")
            self.assertIn("target=XRP", repaired_cfg["impact"]["api_url"])
            self.assertIn("127.0.0.1:8025", repaired_cfg["impact"]["api_url"])
            self.assertEqual(repaired_cfg["control"]["port"], 8024)
            self.assertEqual(repaired_cfg["live"]["symbol"], "XRP/USDC")
            self.assertEqual(
                repaired_cfg["journal"]["db_path"],
                "logs/journal_live_binance_xrp_usdc_rotation.db",
            )
            self.assertEqual(
                repaired_cfg["journal"]["json_path"],
                "logs/journal_live_binance_xrp_usdc_rotation.jsonl",
            )

    def test_expected_runtime_notional_uses_equity_and_min_entry_floor(self) -> None:
        snapshot = {
            "cash_eur": 2.0,
            "position_value_eur": 34.99,
        }

        expected = rotation_apply_active_lanes._expected_runtime_notional(
            snapshot,
            expected_fraction=0.055,
            min_entry_notional_eur=6.0,
        )

        self.assertAlmostEqual(expected, 6.0)

    def test_runtime_fraction_mismatch_accepts_min_entry_floor(self) -> None:
        snapshot = {
            "cash_eur": 36.99,
            "position_value_eur": 0.0,
            "max_exposure_eur": 6.0,
            "cycle_trade_eur": 6.0,
        }

        with mock.patch.object(
            rotation_apply_active_lanes,
            "_configured_min_entry_notional",
            return_value=6.0,
        ):
            mismatch = rotation_apply_active_lanes._runtime_fraction_mismatch(
                "WLFI",
                snapshot,
                0.055,
            )

        self.assertFalse(mismatch)

    def test_should_defer_runtime_update_when_inventory_open(self) -> None:
        active_snapshot = {
            "position_value_eur": 4.2,
            "position_btc": 0.0,
            "open_orders_count": 0,
            "base_balance_notional_eur": 4.2,
        }
        flat_snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 0,
            "base_balance_notional_eur": 0.0,
        }

        self.assertTrue(
            rotation_apply_active_lanes._should_defer_runtime_update(
                active_snapshot,
                "alpha:\n  type: breakout\n",
                "alpha:\n  type: trend\n",
            )
        )
        self.assertFalse(
            rotation_apply_active_lanes._should_defer_runtime_update(
                flat_snapshot,
                "alpha:\n  type: breakout\n",
                "alpha:\n  type: trend\n",
            )
        )
        self.assertFalse(
            rotation_apply_active_lanes._should_defer_runtime_update(
                active_snapshot,
                "alpha:\n  type: breakout\n",
                "alpha:\n  type: breakout\n",
            )
        )

    def test_should_defer_runtime_update_ignores_dust_position(self) -> None:
        dust_snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.01303,
            "mark_price": 0.020625,
            "open_orders_count": 0,
            "base_balance_notional_eur": 0.00026874375,
        }

        self.assertFalse(
            rotation_apply_active_lanes._should_defer_runtime_update(
                dust_snapshot,
                "alpha:\n  type: breakout\n",
                "alpha:\n  type: continuation\n",
            )
        )

    def test_should_defer_runtime_update_ignores_open_orders_without_position(self) -> None:
        open_orders_only_snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 1,
            "base_balance_notional_eur": 0.0,
        }

        self.assertFalse(
            rotation_apply_active_lanes._should_defer_runtime_update(
                open_orders_only_snapshot,
                "alpha:\n  type: breakout\n",
                "alpha:\n  type: continuation\n",
            )
        )

    def test_cleanup_lane_orphan_processes_preserves_runtime_unit_tree(self) -> None:
        with (
            mock.patch.object(rotation_apply_active_lanes, "_lane_process_pids", return_value={101, 102, 103}),
            mock.patch.object(rotation_apply_active_lanes, "_collect_descendant_pids", return_value={101, 103}),
            mock.patch.object(rotation_apply_active_lanes, "_kill_pid") as kill_pid,
        ):
            removed = rotation_apply_active_lanes._cleanup_lane_orphan_processes(
                "APT",
                preserve_root_pid=101,
            )

        self.assertEqual(removed, [102])
        kill_pid.assert_called_once_with(102)

    def test_main_defers_runtime_reload_for_running_lane_with_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            active_path = tmp_path / "rotation_active_lanes.json"
            base_path = tmp_path / "live_binance_bananas31_usdc_rotation.yaml"
            runtime_path = tmp_path / "bananas31_runtime.yaml"
            active_path.write_text(
                json.dumps(
                    {
                        "selected": ["BANANAS31"],
                        "watch_symbols": ["BANANAS31"],
                        "selected_strategy_map": {"BANANAS31": "relative_strength"},
                        "fraction": 0.25,
                    }
                ),
                encoding="utf-8",
            )
            base_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {
                            "type": "breakout",
                            "continuation": {},
                            "trend": {},
                            "breakout": {},
                            "swing": {},
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            runtime_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {"type": "breakout"},
                        "runtime": {
                            "rotation_strategy_name": "breakout",
                            "rotation_alpha_type": "breakout",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            previous_runtime = runtime_path.read_text(encoding="utf-8")
            fake_lanes = {
                "BANANAS31": {
                    "slug": "bananas31",
                    "ports": (8123,),
                    "config": "configs/live_binance_bananas31_usdc_rotation.yaml",
                }
            }
            output = io.StringIO()

            with (
                mock.patch.object(rotation_apply_active_lanes, "ACTIVE_FILE", active_path),
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(rotation_apply_active_lanes, "POOL", ["BANANAS31"]),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_base_config_path",
                    return_value=base_path,
                ),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_runtime_config_path",
                    return_value=runtime_path,
                ),
                mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_snapshot",
                    return_value={
                        "position_value_eur": 4.2,
                        "position_btc": 0.0,
                        "open_orders_count": 0,
                        "base_balance_notional_eur": 4.2,
                    },
                ),
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_active"),
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_watch_only"),
                mock.patch.object(rotation_apply_active_lanes, "_stop_lane"),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_compute_selected_shared_equity",
                    return_value=(0.0, {}),
                ),
                mock.patch.object(rotation_apply_active_lanes, "_unit_state", return_value=("active", "running")),
                mock.patch.object(rotation_apply_active_lanes, "_lane_runtime_config_bootstrapped", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_start_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_reload_lane") as reload_lane,
                mock.patch.object(sys, "argv", ["rotation_apply_active_lanes.py"]),
                mock.patch("sys.stdout", output),
            ):
                rotation_apply_active_lanes.main()

            self.assertEqual(runtime_path.read_text(encoding="utf-8"), previous_runtime)
            self.assertFalse(reload_lane.called)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["deferred_runtime_updates"][0]["symbol"], "BANANAS31")
            self.assertEqual(payload["deferred_runtime_updates"][0]["current_alpha"], "breakout")
            self.assertEqual(payload["deferred_runtime_updates"][0]["desired_alpha"], "trend")

    def test_main_cleans_orphans_for_bootstrapped_running_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            active_path = tmp_path / "rotation_active_lanes.json"
            base_path = tmp_path / "live_binance_apt_usdc_rotation.yaml"
            runtime_path = tmp_path / "apt_runtime.yaml"
            active_path.write_text(
                json.dumps(
                    {
                        "selected": [],
                        "watch_symbols": ["APT"],
                        "selected_strategy_map": {},
                        "fraction": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            base_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {
                            "type": "continuation",
                            "continuation": {},
                            "trend": {},
                            "breakout": {},
                            "swing": {},
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            runtime_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {"type": "swing"},
                        "runtime": {
                            "rotation_strategy_name": "rebound",
                            "rotation_alpha_type": "swing",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            fake_lanes = {
                "APT": {
                    "slug": "apt",
                    "ports": (8036,),
                    "config": "configs/live_binance_apt_usdc_rotation.yaml",
                }
            }
            output = io.StringIO()

            with (
                mock.patch.object(rotation_apply_active_lanes, "ACTIVE_FILE", active_path),
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(rotation_apply_active_lanes, "POOL", ["APT"]),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_base_config_path",
                    return_value=base_path,
                ),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_runtime_config_path",
                    return_value=runtime_path,
                ),
                mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_snapshot",
                    return_value={
                        "position_value_eur": 0.0,
                        "position_btc": 0.0,
                        "open_orders_count": 0,
                        "base_balance_notional_eur": 0.0,
                    },
                ),
                mock.patch.object(rotation_apply_active_lanes, "_unit_state", return_value=("active", "running")),
                mock.patch.object(rotation_apply_active_lanes, "_lane_runtime_config_bootstrapped", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_unit_main_pid", return_value=777),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_cleanup_lane_orphan_processes",
                    return_value=[555],
                ) as cleanup_lane,
                mock.patch.object(rotation_apply_active_lanes, "_reload_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_start_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_stop_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_active"),
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_watch_only", return_value=False),
                mock.patch.object(sys, "argv", ["rotation_apply_active_lanes.py"]),
                mock.patch("sys.stdout", output),
            ):
                rotation_apply_active_lanes.main()

        cleanup_lane.assert_called_once_with("APT", preserve_root_pid=777)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["orphan_process_cleanup"][0]["symbol"], "APT")
        self.assertEqual(payload["orphan_process_cleanup"][0]["killed_pids"], [555])

    def test_set_lane_active_skips_runtime_restart_while_inventory_open(self) -> None:
        fake_lanes = {
            "BANANAS31": {
                "slug": "bananas31",
                "ports": (8123,),
                "config": "configs/live_binance_bananas31_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 3.5,
            "position_btc": 0.0,
            "open_orders_count": 0,
            "base_balance_notional_eur": 3.5,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", return_value=snapshot),
            mock.patch.object(rotation_apply_active_lanes, "_runtime_fraction_mismatch", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_restart_lane_runtime") as restart_lane,
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled", return_value=True),
        ):
            rotation_apply_active_lanes._set_lane_active("BANANAS31", 0.25)

        self.assertFalse(restart_lane.called)
        self.assertTrue(post_control.called)

    def test_set_lane_active_restarts_when_only_open_orders_are_present(self) -> None:
        fake_lanes = {
            "BANANAS31": {
                "slug": "bananas31",
                "ports": (8123,),
                "config": "configs/live_binance_bananas31_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 1,
            "base_balance_notional_eur": 0.0,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", side_effect=[snapshot, snapshot]),
            mock.patch.object(rotation_apply_active_lanes, "_runtime_fraction_mismatch", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_restart_lane_runtime") as restart_lane,
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled", return_value=True),
        ):
            rotation_apply_active_lanes._set_lane_active("BANANAS31", 0.25)

        restart_lane.assert_called_once_with("BANANAS31")
        self.assertTrue(post_control.called)

    def test_set_lane_watch_only_does_not_defer_when_only_open_orders_exist(self) -> None:
        fake_lanes = {
            "SIGN": {
                "slug": "sign",
                "ports": (8111,),
                "config": "configs/live_binance_sign_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 1,
            "base_balance_notional_eur": 0.0,
            "trading_enabled": True,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", return_value=snapshot),
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled", return_value=True),
        ):
            deferred = rotation_apply_active_lanes._set_lane_watch_only("SIGN")

        self.assertFalse(deferred)
        self.assertEqual(post_control.call_args_list[0].args[1], "/pause")
        self.assertEqual(post_control.call_args_list[1].args[1], "/cancel_all")

    def test_set_lane_watch_only_defers_when_inventory_open(self) -> None:
        fake_lanes = {
            "SIGN": {
                "slug": "sign",
                "ports": (8111,),
                "config": "configs/live_binance_sign_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 5.9,
            "position_btc": 127.0,
            "open_orders_count": 0,
            "base_balance_notional_eur": 5.9,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", return_value=snapshot),
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled") as wait_enabled,
        ):
            deferred = rotation_apply_active_lanes._set_lane_watch_only("SIGN")

        self.assertTrue(deferred)
        self.assertFalse(post_control.called)
        self.assertFalse(wait_enabled.called)

    def test_set_lane_watch_only_does_not_defer_on_dust_inventory(self) -> None:
        fake_lanes = {
            "ZK": {
                "slug": "zk",
                "ports": (8112,),
                "config": "configs/live_binance_zk_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.01303,
            "mark_price": 0.020625,
            "open_orders_count": 0,
            "base_balance_notional_eur": 0.00026874375,
            "trading_enabled": True,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", return_value=snapshot),
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled", return_value=True),
        ):
            deferred = rotation_apply_active_lanes._set_lane_watch_only("ZK")

        self.assertFalse(deferred)
        self.assertEqual(post_control.call_args_list[0].args[1], "/pause")
        self.assertEqual(post_control.call_args_list[1].args[1], "/cancel_all")

    def test_set_lane_watch_only_skips_redundant_pause_when_already_disabled(self) -> None:
        fake_lanes = {
            "SIGN": {
                "slug": "sign",
                "ports": (8111,),
                "config": "configs/live_binance_sign_usdc_rotation.yaml",
            }
        }
        snapshot = {
            "position_value_eur": 0.0,
            "position_btc": 0.0,
            "open_orders_count": 0,
            "trading_enabled": False,
        }

        with (
            mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
            mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
            mock.patch.object(rotation_apply_active_lanes, "_lane_snapshot", return_value=snapshot),
            mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
            mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled") as wait_enabled,
        ):
            deferred = rotation_apply_active_lanes._set_lane_watch_only("SIGN")

        self.assertFalse(deferred)
        self.assertFalse(post_control.called)
        self.assertFalse(wait_enabled.called)

    def test_main_defers_watch_only_flatten_for_open_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            active_path = tmp_path / "rotation_active_lanes.json"
            base_path = tmp_path / "live_binance_sign_usdc_rotation.yaml"
            runtime_path = tmp_path / "sign_runtime.yaml"
            active_path.write_text(
                json.dumps(
                    {
                        "selected": [],
                        "watch_symbols": ["SIGN"],
                        "selected_strategy_map": {},
                        "fraction": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            base_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {
                            "type": "continuation",
                            "continuation": {},
                            "trend": {},
                            "breakout": {},
                            "swing": {},
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            runtime_path.write_text(
                yaml.safe_dump(
                    {
                        "alpha": {"type": "breakout"},
                        "runtime": {
                            "rotation_strategy_name": "breakout",
                            "rotation_alpha_type": "breakout",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            fake_lanes = {
                "SIGN": {
                    "slug": "sign",
                    "ports": (8111,),
                    "config": "configs/live_binance_sign_usdc_rotation.yaml",
                }
            }
            output = io.StringIO()

            with (
                mock.patch.object(rotation_apply_active_lanes, "ACTIVE_FILE", active_path),
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(rotation_apply_active_lanes, "POOL", ["SIGN"]),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_base_config_path",
                    return_value=base_path,
                ),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_runtime_config_path",
                    return_value=runtime_path,
                ),
                mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_snapshot",
                    return_value={
                        "position_value_eur": 5.9,
                        "position_btc": 127.0,
                        "open_orders_count": 0,
                        "base_balance_notional_eur": 5.9,
                    },
                ),
                mock.patch.object(rotation_apply_active_lanes, "_unit_state", return_value=("active", "running")),
                mock.patch.object(rotation_apply_active_lanes, "_lane_runtime_config_bootstrapped", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_start_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_reload_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_stop_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_active"),
                mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
                mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled"),
                mock.patch.object(sys, "argv", ["rotation_apply_active_lanes.py"]),
                mock.patch("sys.stdout", output),
            ):
                rotation_apply_active_lanes.main()

            self.assertFalse(post_control.called)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["deferred_watch_only"][0]["symbol"], "SIGN")
            self.assertEqual(payload["deferred_watch_only"][0]["reason"], "inventory_open")

    def test_main_keeps_non_watch_lane_running_when_inventory_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            active_path = tmp_path / "rotation_active_lanes.json"
            active_path.write_text(
                json.dumps(
                    {
                        "selected": [],
                        "watch_symbols": [],
                        "selected_strategy_map": {},
                        "fraction": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            fake_lanes = {
                "SIGN": {
                    "slug": "sign",
                    "ports": (8111,),
                    "config": "configs/live_binance_sign_usdc_rotation.yaml",
                }
            }
            output = io.StringIO()

            with (
                mock.patch.object(rotation_apply_active_lanes, "ACTIVE_FILE", active_path),
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(rotation_apply_active_lanes, "POOL", ["SIGN"]),
                mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=True),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_snapshot",
                    return_value={
                        "position_value_eur": 7.2,
                        "position_btc": 120.0,
                        "open_orders_count": 0,
                        "base_balance_notional_eur": 7.2,
                        "trading_enabled": True,
                    },
                ),
                mock.patch.object(rotation_apply_active_lanes, "_unit_state", return_value=("active", "running")),
                mock.patch.object(rotation_apply_active_lanes, "_lane_runtime_config_bootstrapped", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_start_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_reload_lane"),
                mock.patch.object(rotation_apply_active_lanes, "_stop_lane") as stop_lane,
                mock.patch.object(rotation_apply_active_lanes, "_set_lane_active"),
                mock.patch.object(rotation_apply_active_lanes, "_post_control") as post_control,
                mock.patch.object(rotation_apply_active_lanes, "_wait_lane_trading_enabled"),
                mock.patch.object(sys, "argv", ["rotation_apply_active_lanes.py"]),
                mock.patch("sys.stdout", output),
            ):
                rotation_apply_active_lanes.main()

            self.assertFalse(stop_lane.called)
            self.assertFalse(post_control.called)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["inventory_protected_watch_symbols"], ["SIGN"])
            self.assertIn("SIGN", payload["watch_symbols"])
            self.assertEqual(payload["deferred_watch_only"][0]["symbol"], "SIGN")
            self.assertEqual(payload["deferred_watch_only"][0]["reason"], "inventory_open")

    def test_lane_inventory_protection_ignores_dust_without_open_orders(self) -> None:
        with mock.patch.object(rotation_apply_active_lanes, "INVENTORY_PROTECT_MIN_NOTIONAL_EUR", 1.0):
            dust_snapshot = {
                "position_value_eur": 0.0,
                "position_btc": 0.0,
                "mark_price": 1.0,
                "base_balance_notional_eur": 0.42,
                "open_orders_count": 0,
            }
            self.assertFalse(rotation_apply_active_lanes._lane_has_position_inventory(dust_snapshot))
            self.assertFalse(rotation_apply_active_lanes._lane_has_active_inventory(dust_snapshot))

            order_snapshot = dict(dust_snapshot, open_orders_count=1)
            self.assertFalse(rotation_apply_active_lanes._lane_has_position_inventory(order_snapshot))
            self.assertTrue(rotation_apply_active_lanes._lane_has_active_inventory(order_snapshot))

            real_position_snapshot = dict(dust_snapshot, base_balance_notional_eur=1.01)
            self.assertTrue(rotation_apply_active_lanes._lane_has_position_inventory(real_position_snapshot))
            self.assertTrue(rotation_apply_active_lanes._lane_has_active_inventory(real_position_snapshot))

    def test_start_lane_falls_back_to_restart_when_transient_unit_is_still_loaded(self) -> None:
        fake_lanes = {
            "BANANAS31": {
                "slug": "bananas31",
                "ports": (8123, 9123, 9223, 9323, 9423),
                "config": "configs/live_binance_bananas31_usdc_rotation.yaml",
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_path = Path(tmpdir) / "bananas31_runtime.yaml"
            runtime_path.write_text("alpha:\n  type: breakout\n", encoding="utf-8")
            run_error = subprocess.CalledProcessError(1, ["systemd-run"])
            with (
                mock.patch.object(rotation_apply_active_lanes, "LANES", fake_lanes),
                mock.patch.object(
                    rotation_apply_active_lanes,
                    "_lane_runtime_config_path",
                    return_value=runtime_path,
                ),
                mock.patch.object(rotation_apply_active_lanes, "_lane_runtime_config_bootstrapped", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_unit_state", return_value=("", "")),
                mock.patch.object(rotation_apply_active_lanes, "_wait_unit_released", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_unit_load_state", return_value="loaded"),
                mock.patch.object(rotation_apply_active_lanes, "_wait_control_ready", return_value=True),
                mock.patch.object(rotation_apply_active_lanes, "_http_ok", return_value=False),
                mock.patch.object(rotation_apply_active_lanes.subprocess, "check_call", side_effect=run_error) as check_call,
                mock.patch.object(rotation_apply_active_lanes.subprocess, "run") as run_cmd,
            ):
                rotation_apply_active_lanes._start_lane("BANANAS31", force_recreate=True)

        restart_calls = [
            call
            for call in run_cmd.call_args_list
            if call.args and call.args[0][:4] == ["systemctl", "--user", "restart", "codex-rotation-bananas31.service"]
        ]
        self.assertEqual(check_call.call_count, 1)
        self.assertTrue(restart_calls)


if __name__ == "__main__":
    unittest.main()
