from __future__ import annotations

import unittest
from unittest.mock import patch

import robinhood_radar


class RobinhoodRadarTests(unittest.TestCase):
    def test_market_uses_exact_pool_and_preserves_zero(self):
        asset, pool = "0x" + "11" * 20, "0x" + "22" * 20
        pair = {"chainId": "robinhood", "pairAddress": pool, "baseToken": {"address": asset},
                "marketCap": 70000, "fdv": 999999, "priceUsd": "0.000047092",
                "priceChange": {"h24": -89.79}, "volume": {"h24": 0}, "liquidity": {"usd": 12000}}
        def response(url):
            return {"pairs": [dict(pair, chainId="ethereum"), pair]} if "dexscreener" in url else {"data": {"id": "robinhood_" + asset, "attributes": {"address": asset, "holders": {"count": 1557}}}}
        with patch.object(robinhood_radar, "_get_json", side_effect=response):
            data = robinhood_radar.market_data(asset, pool)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["volume_24h"], 0)
        self.assertEqual(data["market_cap"], 70000)
        self.assertEqual(data["holders"], 1557)
        self.assertEqual(data["change_24h"], -89.79)

    def test_market_rejects_wrong_chain_pool_and_quote_direction(self):
        asset, pool = "0x" + "11" * 20, "0x" + "22" * 20
        for pair in [
            {"chainId": "ethereum", "pairAddress": pool, "baseToken": {"address": asset}},
            {"chainId": "robinhood", "pairAddress": asset, "baseToken": {"address": asset}},
            {"chainId": "robinhood", "pairAddress": pool, "baseToken": {"address": pool}},
        ]:
            with patch.object(robinhood_radar, "_get_json", return_value={"pairs": [dict(pair, priceUsd="5")]}):
                self.assertIsNone(robinhood_radar.market_data(asset, pool)["price_usd"])

    def test_market_failure_and_invalid_address(self):
        with patch.object(robinhood_radar, "_get_json", side_effect=OSError("offline")):
            data = robinhood_radar.market_data("0x" + "11" * 20, "0x" + "22" * 20)
        self.assertEqual(data["status"], "unavailable")
        self.assertIsNone(data["volume_24h"])
        self.assertEqual(len(data["notes"]), 2)
        with self.assertRaises(ValueError):
            robinhood_radar.market_data("../../bad", "bad")

    def test_decodes_v2_and_v3_pool_events(self):
        token0 = "11" * 20
        token1 = "22" * 20
        pair = "33" * 20
        pool = "44" * 20
        logs = [
            {
                "topics": [
                    robinhood_radar.PAIR_CREATED_TOPIC,
                    "0x" + "00" * 12 + token0,
                    "0x" + "00" * 12 + token1,
                ],
                "data": "0x" + "00" * 12 + pair + "00" * 32,
                "blockNumber": "0x10",
                "transactionHash": "0xabc",
            },
            {
                "topics": [
                    robinhood_radar.POOL_CREATED_TOPIC,
                    "0x" + "00" * 12 + token0,
                    "0x" + "00" * 12 + token1,
                    "0x" + (10_000).to_bytes(32, "big").hex(),
                ],
                "data": "0x" + (200).to_bytes(32, "big").hex() + "00" * 12 + pool,
                "blockNumber": "0x11",
                "transactionHash": "0xdef",
            },
        ]
        events = robinhood_radar._decode_events(logs)
        self.assertEqual(events[0]["venue"], "Uniswap V3")
        self.assertEqual(events[0]["pool"], "0x" + pool)
        self.assertEqual(events[0]["fee_pct"], 1.0)
        self.assertEqual(events[1]["venue"], "Uniswap V2")
        self.assertEqual(events[1]["pool"], "0x" + pair)

    def test_decodes_dynamic_and_bytes32_strings(self):
        dynamic = "0x" + (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex() + b"MEME".hex().ljust(64, "0")
        fixed = "0x" + b"WETH".hex().ljust(64, "0")
        self.assertEqual(robinhood_radar._abi_string(dynamic), "MEME")
        self.assertEqual(robinhood_radar._abi_string(fixed), "WETH")

    def test_unknown_quote_is_never_marked_low_risk(self):
        event = {
            "venue": "Uniswap V3",
            "pool": "0x" + "33" * 20,
            "token0": "0x" + "11" * 20,
            "token1": "0x" + "22" * 20,
            "block_number": 100,
            "fee_pct": 1,
            "tx_hash": "0xabc",
        }
        metadata = {
            event["token0"]: {"name": "Test", "symbol": "TEST", "decimals": 18, "total_supply": 10**27, "code_bytes": 500},
            event["token1"]: {"name": "Other", "symbol": "OTHER", "decimals": 18, "total_supply": 10**27, "code_bytes": 500},
        }
        item = robinhood_radar._enrich_event(event, metadata, {100: 900}, {}, 1000)
        self.assertEqual(item["risk_level"], "high")
        self.assertIn("非标准报价资产", item["flags"])


if __name__ == "__main__":
    unittest.main()
