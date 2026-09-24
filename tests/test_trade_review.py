import unittest
from unittest import mock

import trade_review


class TradeReviewTests(unittest.TestCase):
    def test_order_metadata_supplies_execution_side_and_type(self):
        rows = trade_review.merge_order_metadata(
            [{"order_id": "1", "symbol": "ABC", "side": ""}],
            [{"order_id": "1", "side": "Buy", "order_type": "Limit"}],
        )
        self.assertEqual(rows[0]["side"], "Buy")
        self.assertEqual(rows[0]["order_type"], "Limit")

    def test_executions_are_paired_into_realized_round_trips(self):
        trades = trade_review.round_trips_from_executions([
            {"symbol": "ABC", "side": "Buy", "quantity": 100, "price": 10, "trade_done_at": "2026-09-01T13:36:00+00:00"},
            {"symbol": "ABC", "side": "Sell", "quantity": 100, "price": 9.5, "trade_done_at": "2026-09-01T13:44:00+00:00"},
        ])

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pnl"], -50.0)
        self.assertEqual(trades[0]["entry_time"], "09:36")

    def test_review_reports_payoff_and_dominant_failure_categories(self):
        report = trade_review.build_trade_review([
            {"symbol": "GOOD", "pnl": 100, "side": "round_trip", "entry_time": "09:37", "entry_type": "LIMIT", "spread_pct": 0.4},
            {"symbol": "LATE", "pnl": -350, "side": "round_trip", "entry_time": "09:31", "entry_type": "MARKET", "spread_pct": 1.8},
            {"symbol": "TINY", "pnl": -400, "side": "round_trip", "entry_time": "09:42", "entry_type": "MARKET", "spread_pct": 2.1, "market_cap": 30_000_000},
        ])

        self.assertEqual(report["summary"]["win_rate"], 33.3)
        self.assertEqual(report["summary"]["average_win"], 100.0)
        self.assertEqual(report["summary"]["average_loss"], -375.0)
        self.assertIn("开盘确认不足", report["trades"][1]["issues"])
        self.assertIn("市价单滑点风险", report["trades"][2]["issues"])
        self.assertIn("选股质量不足", report["trades"][2]["issues"])

    def test_missing_longbridge_config_is_an_empty_not_connected_review(self):
        review = trade_review.not_connected_review()
        self.assertFalse(review["connected"])
        self.assertEqual(review["code"], "not_connected")
        self.assertEqual(review["summary"]["trade_count"], 0)
        self.assertEqual(review["trades"], [])
        with mock.patch("momentum_scanner._client_id", side_effect=RuntimeError("未找到 Longbridge OAuth 配置")):
            self.assertFalse(trade_review.longbridge_configured())
        self.assertTrue(trade_review.looks_unconfigured(RuntimeError("Longbridge 授权已失效")))


if __name__ == "__main__":
    unittest.main()
