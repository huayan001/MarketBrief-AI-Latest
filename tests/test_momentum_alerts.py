import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import momentum_alerts


class MomentumAlertTests(unittest.TestCase):
    def test_next_scan_uses_fixed_eastern_opening_slots(self):
        before_open = datetime(2026, 9, 1, 13, 24, tzinfo=timezone.utc)
        after_first_slot = datetime(2026, 9, 1, 13, 26, tzinfo=timezone.utc)
        after_window = datetime(2026, 9, 1, 13, 51, tzinfo=timezone.utc)

        self.assertEqual(
            momentum_alerts.next_scan_at(before_open),
            datetime(2026, 9, 1, 13, 25, tzinfo=timezone.utc),
        )
        self.assertEqual(
            momentum_alerts.next_scan_at(after_first_slot),
            datetime(2026, 9, 1, 13, 35, tzinfo=timezone.utc),
        )
        self.assertEqual(
            momentum_alerts.next_scan_at(after_window),
            datetime(2026, 9, 2, 13, 25, tzinfo=timezone.utc),
        )

    def test_next_scan_observes_winter_offset_and_skips_weekends(self):
        friday_after_window = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(
            momentum_alerts.next_scan_at(friday_after_window),
            datetime(2026, 1, 5, 14, 25, tzinfo=timezone.utc),
        )

    def test_schedule_status_marks_only_opening_window_as_recommended(self):
        inside = momentum_alerts.schedule_status(datetime(2026, 9, 1, 13, 40, tzinfo=timezone.utc))
        outside = momentum_alerts.schedule_status(datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc))
        self.assertTrue(inside["in_recommended_window"])
        self.assertFalse(outside["in_recommended_window"])
        self.assertEqual(inside["timezone"], "America/New_York")

    @staticmethod
    def _claim_store():
        claimed = set()

        def claim(user_id, symbol, _cooldown):
            key = (user_id, symbol)
            if key in claimed:
                return False
            claimed.add(key)
            return True

        def release(user_id, symbol):
            claimed.discard((user_id, symbol))

        return claim, release

    def test_new_signal_is_sent_once_during_cooldown(self):
        scan = mock.Mock(return_value={"provider": "Longbridge OpenAPI", "candidates": [{
            "symbol": "TEST", "alert": True, "score": 75, "price": 2.5,
            "change_pct": 20.0, "float_shares": 2_000_000, "volume_float_ratio": 3.0,
        }]})
        send = mock.Mock(return_value=1)
        claim, release = self._claim_store()
        runner = momentum_alerts.MomentumAlertRunner(scan=scan, send=send, claim=claim, release=release)
        recipients = [{"user_id": 1, "config": {"bot_token": "t", "chat_id": "c"}}]
        with mock.patch.dict(os.environ, {"MOMENTUM_TELEGRAM_ENABLED": "true"}), \
             mock.patch.object(runner, "_telegram_recipients", return_value=recipients):
            self.assertEqual(runner.run_once()["sent"], 1)
            self.assertEqual(runner.run_once()["sent"], 0)
        send.assert_called_once()

    def test_duplicate_is_suppressed_across_runner_instances(self):
        scan = mock.Mock(return_value={"provider": "Longbridge OpenAPI", "candidates": [{
            "symbol": "TEST", "alert": True, "score": 75, "price": 2.5,
            "change_pct": 20.0, "float_shares": 2_000_000, "volume_float_ratio": 3.0,
        }]})
        send = mock.Mock(return_value=1)
        recipients = [{"user_id": 1, "config": {"bot_token": "t", "chat_id": "c"}}]
        claim, release = self._claim_store()
        first = momentum_alerts.MomentumAlertRunner(scan=scan, send=send, claim=claim, release=release)
        second = momentum_alerts.MomentumAlertRunner(scan=scan, send=send, claim=claim, release=release)
        with mock.patch.dict(os.environ, {"MOMENTUM_TELEGRAM_ENABLED": "true"}), \
             mock.patch.object(first, "_telegram_recipients", return_value=recipients), \
             mock.patch.object(second, "_telegram_recipients", return_value=recipients):
            self.assertEqual(first.run_once()["sent"], 1)
            self.assertEqual(second.run_once()["sent"], 0)
        send.assert_called_once()

    def test_risk_gate_blocks_alert_after_two_consecutive_losses(self):
        scan = mock.Mock()
        runner = momentum_alerts.MomentumAlertRunner(
            scan=scan,
            risk_state=lambda: {"consecutive_losses": 2, "daily_loss_pct": 0},
        )
        with mock.patch.dict(os.environ, {"MOMENTUM_TELEGRAM_ENABLED": "true"}), \
             mock.patch.object(runner, "_telegram_recipients", return_value=[{"user_id": 1, "config": {"bot_token": "t", "chat_id": "c"}}]):
            result = runner.run_once()

        self.assertEqual(result["reason"], "risk_gate")
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
