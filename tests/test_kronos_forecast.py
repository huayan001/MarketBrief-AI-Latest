from __future__ import annotations

import json
import unittest
from unittest import mock

import kronos_forecast


class KronosForecastTests(unittest.TestCase):
    def tearDown(self):
        kronos_forecast._CATALOG_CACHE = None

    def test_equity_contract_is_mapped_to_xyz(self):
        self.assertEqual(kronos_forecast.normalize_contract("NVDA-USDC"), "xyz:NVDA")
        self.assertEqual(kronos_forecast.normalize_contract("btc-usdc"), "BTC")

    def test_predict_clamps_user_controlled_work(self):
        output = {
            "symbol": "BTC",
            "forecast": [{"timestamp": "2026-09-03 01:00:00", "close": 100}],
            "expected_change_pct": 0,
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(output), stderr="")
        with (
            mock.patch.object(kronos_forecast, "status", return_value={"ready": True}),
            mock.patch.object(kronos_forecast.subprocess, "run", return_value=completed) as run,
        ):
            result = kronos_forecast.predict({"symbol": "BTC", "pred_len": 999, "samples": 999})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--pred-len") + 1], "12")
        self.assertEqual(command[command.index("--samples") + 1], "20")
        self.assertEqual(result["model"], "Kronos-small")
        self.assertEqual(result["forecast"][0]["timestamp"], "2026-09-03T01:00:00Z")

    def test_catalog_groups_live_perps_and_xyz_tradfi(self):
        responses = [
            {"universe": [{"name": "BTC"}, {"name": "OLD", "isDelisted": True}]},
            {"universe": [{"name": "xyz:NVDA"}, {"name": "xyz:GOLD"}]},
        ]
        with mock.patch.object(kronos_forecast, "_post_info", side_effect=responses):
            catalog = kronos_forecast.contract_catalog(force=True)
        self.assertEqual([group["key"] for group in catalog["groups"]], ["perps", "tradfi"])
        self.assertEqual(catalog["groups"][0]["items"], [
            {"value": "BTC", "label": "BTC-USDC", "search": "BTC BTC"},
        ])
        self.assertEqual([item["value"] for item in catalog["groups"][1]["items"]], ["xyz:NVDA", "xyz:GOLD"])
        self.assertEqual(catalog["refresh_policy"], "process_start")

    def test_catalog_is_reused_until_process_restart(self):
        responses = [{"universe": [{"name": "BTC"}]}, {"universe": [{"name": "xyz:GOLD"}]}]
        with mock.patch.object(kronos_forecast, "_post_info", side_effect=responses) as post:
            first = kronos_forecast.contract_catalog(force=True)
            second = kronos_forecast.contract_catalog()
        self.assertIs(first, second)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
