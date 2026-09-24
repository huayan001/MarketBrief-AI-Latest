import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import momentum_strategy


ET = ZoneInfo("America/New_York")


class MomentumStrategyTests(unittest.TestCase):
    def test_candidate_must_pass_liquidity_quality_filters(self):
        accepted = momentum_strategy.evaluate_candidate({
            "price": 8.0, "volume": 2_000_000, "market_cap": 160_000_000,
            "bid": 7.98, "ask": 8.02, "change_pct": 14.0,
        })
        rejected = momentum_strategy.evaluate_candidate({
            "price": 1.2, "volume": 8_000_000, "market_cap": 30_000_000,
            "bid": 1.1, "ask": 1.3, "change_pct": 40.0,
        })

        self.assertTrue(accepted["eligible"])
        self.assertFalse(rejected["eligible"])
        self.assertIn("price", rejected["rejection_reasons"])
        self.assertIn("market_cap", rejected["rejection_reasons"])
        self.assertIn("spread", rejected["rejection_reasons"])

    def test_opening_decision_waits_then_requires_closed_bar_confirmation(self):
        self.assertEqual(
            momentum_strategy.opening_decision(datetime(2026, 9, 1, 9, 28, tzinfo=ET))["phase"],
            "candidate_pool",
        )
        waiting = momentum_strategy.opening_decision(
            datetime(2026, 9, 1, 9, 33, tzinfo=ET),
            breakout=True, volume_confirmed=True, bar_closed=True,
        )
        confirmed = momentum_strategy.opening_decision(
            datetime(2026, 9, 1, 9, 36, tzinfo=ET),
            breakout=True, volume_confirmed=True, bar_closed=True,
        )
        self.assertFalse(waiting["notify"])
        self.assertTrue(confirmed["notify"])

    def test_risk_gate_stops_after_two_losses_or_daily_limit(self):
        self.assertFalse(momentum_strategy.risk_gate(consecutive_losses=2, daily_loss_pct=0.05)["allow_new_alerts"])
        self.assertFalse(momentum_strategy.risk_gate(consecutive_losses=0, daily_loss_pct=0.30)["allow_new_alerts"])
        self.assertTrue(momentum_strategy.risk_gate(consecutive_losses=1, daily_loss_pct=0.05)["allow_new_alerts"])


if __name__ == "__main__":
    unittest.main()
