import unittest


class TestExecGUIOrderNotional(unittest.TestCase):
    def test_fill_notional_is_computed(self) -> None:
        # Import from the module source path; this is used by the standalone exec GUI.
        from modules.exec.src.trading_exec.gui import _build_order_view  # type: ignore

        events = [
            {
                "ts": "2026-02-14T00:00:00+00:00",
                "event_type": "exec_report",
                "payload": {"ts": "2026-02-14T00:00:00+00:00", "order_id": "o1", "status": "OPEN"},
            },
            {
                "ts": "2026-02-14T00:00:01+00:00",
                "event_type": "fill",
                "payload": {
                    "ts": "2026-02-14T00:00:01+00:00",
                    "order_id": "o1",
                    "qty_btc": 0.01,
                    "price": 50000.0,
                    "fee_eur": 1.0,
                },
            },
        ]

        rows, issues = _build_order_view(events)
        self.assertEqual(issues, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "o1")
        self.assertAlmostEqual(rows[0]["total_fill_qty_btc"], 0.01, places=12)
        self.assertAlmostEqual(rows[0]["total_fill_notional_eur"], 500.0, places=2)
        self.assertAlmostEqual(rows[0]["avg_fill_price_eur"], 50000.0, places=2)

    def test_budget_cutoff_filters_old_events(self) -> None:
        from modules.exec.src.trading_exec.gui import build_exec_snapshot  # type: ignore

        status = {"data": {"exec": {"mode": "sim"}, "core": {"trading_enabled": True}}}
        journal = [
            # old fill (should be filtered)
            {"ts": "2026-02-14T00:00:01+00:00", "event_type": "fill", "payload": {"order_id": "old", "qty_btc": 0.01, "price": 50000.0, "fee_eur": 1.0}},
            # cutoff
            {"ts": "2026-02-14T00:00:02+00:00", "event_type": "core_budget_reset", "payload": {"cutoff_ts": "2026-02-14T00:00:02+00:00"}},
            # new fill (should remain)
            {"ts": "2026-02-14T00:00:03+00:00", "event_type": "fill", "payload": {"order_id": "new", "qty_btc": 0.01, "price": 50000.0, "fee_eur": 1.0}},
        ]
        snap = build_exec_snapshot(status, journal)
        orders = snap.get("orders") or []
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "new")
        self.assertEqual(snap.get("core", {}).get("budget_cutoff_ts"), "2026-02-14T00:00:02+00:00")

    def test_budget_cutoff_from_core_telemetry_filters_when_journal_window_is_short(self) -> None:
        from modules.exec.src.trading_exec.gui import build_exec_snapshot  # type: ignore

        status = {
            "data": {
                "exec": {"mode": "sim"},
                "core": {
                    "trading_enabled": True,
                    "budget_cutoff_ts": "2026-02-14T00:00:02+00:00",
                },
            }
        }
        journal = [
            # Simulate short window: contains fills only, no core_budget_reset event anymore.
            {"ts": "2026-02-14T00:00:01+00:00", "event_type": "fill", "payload": {"order_id": "old", "qty_btc": 0.01, "price": 50000.0, "fee_eur": 1.0}},
            {"ts": "2026-02-14T00:00:03+00:00", "event_type": "fill", "payload": {"order_id": "new", "qty_btc": 0.01, "price": 50000.0, "fee_eur": 1.0}},
        ]
        snap = build_exec_snapshot(status, journal)
        orders = snap.get("orders") or []
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "new")
        self.assertEqual(snap.get("core", {}).get("budget_cutoff_ts"), "2026-02-14T00:00:02+00:00")


if __name__ == "__main__":
    unittest.main()
