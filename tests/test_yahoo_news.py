from __future__ import annotations

import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

import server


def _story(
    title: str,
    link: str,
    *,
    publisher: str = "Example Wire",
    summary: str = "",
    published: int | None = None,
) -> dict:
    item = {
        "title": title,
        "publisher": publisher,
        "link": link,
        "summary": summary,
        "type": "STORY",
    }
    if published is not None:
        item["providerPublishTime"] = published
    return item


def _query_from_path(path: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["q"][0]


def _chart_result(short_name: str, long_name: str) -> dict:
    count = 80
    start = 1_700_000_000
    timestamps = [start + index * 86_400 for index in range(count)]
    closes = [100.0 + index * 0.4 for index in range(count)]
    return {
        "meta": {
            "shortName": short_name,
            "longName": long_name,
            "currency": "HKD",
            "exchangeName": "HKG",
            "instrumentType": "EQUITY",
        },
        "timestamp": timestamps,
        "indicators": {
            "quote": [
                {
                    "open": closes,
                    "high": [price + 1 for price in closes],
                    "low": [price - 1 for price in closes],
                    "close": closes,
                    "volume": [1_000_000 + index * 100 for index in range(count)],
                }
            ],
            "adjclose": [{"adjclose": closes}],
        },
    }


class YahooNewsQueryTests(unittest.TestCase):
    def test_crypto_and_exchange_suffix_queries(self):
        self.assertEqual(
            server.yahoo_news_queries(
                "BTC-USD",
                {"shortName": "Bitcoin USD", "longName": "Bitcoin USD"},
            ),
            ["BTC-USD", "BTC", "Bitcoin"],
        )
        self.assertEqual(
            server.yahoo_news_queries(
                "ETH-USD",
                {"shortName": "Ethereum USD", "longName": "Ethereum USD"},
            ),
            ["ETH-USD", "ETH", "Ethereum"],
        )
        self.assertEqual(
            server.yahoo_news_queries(
                "0700.HK",
                {"shortName": "TENCENT", "longName": "Tencent Holdings Limited"},
            ),
            ["0700.HK", "TENCENT", "Tencent Holdings Limited"],
        )
        self.assertNotIn("0700", server.yahoo_news_queries("0700.HK", {"shortName": "TENCENT"}))


class YahooNewsFetchTests(unittest.TestCase):
    def _fetch(self, payloads, symbol, meta=None, limit=8):
        calls = []

        def fake_json(hosts, path, attempts_per_host=1):
            query = _query_from_path(path)
            calls.append(query)
            self.assertIn("quotesCount=0", path)
            self.assertIn(f"newsCount={limit}", path)
            self.assertIn("enableFuzzyQuery=false", path)
            self.assertEqual(attempts_per_host, 1)
            if query not in payloads:
                raise AssertionError(query)
            payload = payloads[query]
            if isinstance(payload, Exception):
                raise payload
            return payload

        with mock.patch.object(server, "fetch_json_with_retries", side_effect=fake_json):
            news = server.fetch_yahoo_news(symbol, limit=limit, meta=meta)
        return news, calls

    def test_plain_us_tickers_keep_symbol_news_without_fallback(self):
        published = datetime(2024, 1, 2, 3, 4, tzinfo=timezone.utc)
        for symbol, name in (("AAPL", "Apple Inc."), ("TSLA", "Tesla, Inc.")):
            news, calls = self._fetch(
                {
                    symbol: {
                        "news": [
                            _story(
                                f"{symbol} rises",
                                f"https://news.example/{symbol.lower()}",
                                summary="session recap",
                                published=int(published.timestamp()),
                            )
                        ]
                    },
                    name: {"news": [_story("name fallback", "https://news.example/name")]},
                },
                symbol,
                meta={"shortName": name, "longName": name},
            )
            self.assertEqual(calls, [symbol])
            self.assertEqual(len(news), 1)
            self.assertEqual(news[0]["title"], f"{symbol} rises")
            self.assertEqual(news[0]["publisher"], "Example Wire")
            self.assertEqual(news[0]["summary"], "session recap")
            self.assertEqual(news[0]["published_at"], "2024-01-02 03:04")
            self.assertEqual(news[0]["link"], f"https://news.example/{symbol.lower()}")

    def test_btc_usd_empty_then_base_ticker_and_bitcoin_name(self):
        news, calls = self._fetch(
            {
                "BTC-USD": {"news": []},
                "BTC": {"news": []},
                "Bitcoin": {
                    "news": [
                        _story("Bitcoin rallies", "https://news.example/rally"),
                        _story("Bitcoin rallies", "https://news.example/rally-copy"),
                        _story("Same link other title", "https://news.example/rally"),
                        _story("Miners expand", "https://news.example/miners"),
                        _story("Beyond limit", "https://news.example/extra"),
                    ]
                },
                "Bitcoin USD": {"news": [_story("raw usd name", "https://news.example/raw")]},
            },
            "BTC-USD",
            meta={"shortName": "Bitcoin USD", "longName": "Bitcoin USD"},
            limit=2,
        )
        self.assertEqual(calls, ["BTC-USD", "BTC", "Bitcoin"])
        self.assertEqual([item["title"] for item in news], ["Bitcoin rallies", "Miners expand"])

    def test_btc_base_ticker_fills_the_limit_before_the_name(self):
        stories = [
            _story(f"BTC story {index}", f"https://news.example/btc-{index}")
            for index in range(3)
        ]
        news, calls = self._fetch(
            {
                "BTC-USD": {"news": []},
                "BTC": {"news": stories},
                "Bitcoin": {"news": [_story("name story", "https://news.example/name")]},
            },
            "BTC-USD",
            meta={"shortName": "Bitcoin USD", "longName": "Bitcoin USD"},
            limit=3,
        )
        self.assertEqual(calls, ["BTC-USD", "BTC"])
        self.assertEqual([item["title"] for item in news], [item["title"] for item in stories])

    def test_hk_symbol_empty_then_company_name(self):
        stories = [
            _story("Tencent launches DataBuddy", "https://news.example/tencent"),
            _story("Tencent earnings preview", "https://news.example/tencent-earnings"),
        ]
        news, calls = self._fetch(
            {
                "0700.HK": {"news": []},
                "0700": {"news": [_story("numeric code", "https://news.example/numeric")]},
                "TENCENT": {"news": stories},
                "Tencent Holdings Limited": {
                    "news": [_story("long name", "https://news.example/long")]
                },
            },
            "0700.HK",
            meta={"shortName": "TENCENT", "longName": "Tencent Holdings Limited"},
            limit=2,
        )
        self.assertEqual(calls, ["0700.HK", "TENCENT"])
        self.assertEqual([item["title"] for item in news], [item["title"] for item in stories])

    def test_hk_long_name_is_used_when_short_name_has_no_news(self):
        news, calls = self._fetch(
            {
                "9988.HK": {"news": []},
                "9988": {"news": [_story("numeric code", "https://news.example/numeric")]},
                "BABA-W": {"news": []},
                "Alibaba Group Holding Limited": {
                    "news": [_story("Alibaba probe", "https://news.example/baba")]
                },
            },
            "9988.HK",
            meta={"shortName": "BABA-W", "longName": "Alibaba Group Holding Limited"},
        )
        self.assertEqual(calls, ["9988.HK", "BABA-W", "Alibaba Group Holding Limited"])
        self.assertEqual(news[0]["title"], "Alibaba probe")

    def test_fallback_merges_until_limit_and_dedupes_title_or_link(self):
        news, calls = self._fetch(
            {
                "0700.HK": {"news": []},
                "TENCENT": {"news": [_story("Shared headline", "https://news.example/shared")]},
                "Tencent Holdings Limited": {
                    "news": [
                        _story("Shared headline", "https://news.example/shared-copy"),
                        _story("Cloud expansion", "https://news.example/cloud"),
                    ]
                },
            },
            "0700.HK",
            meta={"shortName": "TENCENT", "longName": "Tencent Holdings Limited"},
            limit=2,
        )
        self.assertEqual(calls, ["0700.HK", "TENCENT", "Tencent Holdings Limited"])
        self.assertEqual([item["title"] for item in news], ["Shared headline", "Cloud expansion"])

    def test_query_error_still_tries_fallback_and_total_failure_is_empty(self):
        news, calls = self._fetch(
            {
                "ETH-USD": RuntimeError("HTTP 500"),
                "ETH": {"news": []},
                "Ethereum": {"news": [_story("Ether ETF", "https://news.example/eth")]},
            },
            "ETH-USD",
            meta={"shortName": "Ethereum USD", "longName": "Ethereum USD"},
        )
        self.assertEqual(calls, ["ETH-USD", "ETH", "Ethereum"])
        self.assertEqual(news[0]["title"], "Ether ETF")

        failed, failed_calls = self._fetch(
            {
                "ETH-USD": RuntimeError("offline"),
                "ETH": RuntimeError("offline"),
                "Ethereum": RuntimeError("offline"),
            },
            "ETH-USD",
            meta={"shortName": "Ethereum USD"},
        )
        self.assertEqual(failed_calls, ["ETH-USD", "ETH", "Ethereum"])
        self.assertEqual(failed, [])

    def test_build_analysis_passes_chart_names_into_news_search(self):
        def fake_json(hosts, path, attempts_per_host=1):
            query = _query_from_path(path)
            if query == "0700.HK":
                return {"news": []}
            if query == "TENCENT":
                return {"news": [_story("Tencent update", "https://news.example/tencent")]}
            if query == "Tencent Holdings Limited":
                return {"news": [_story("Tencent update", "https://news.example/tencent")]}
            raise AssertionError(query)

        with (
            mock.patch.object(server, "fetch_yahoo_chart", return_value=_chart_result("TENCENT", "Tencent Holdings Limited")),
            mock.patch.object(server, "fetch_json_with_retries", side_effect=fake_json),
        ):
            analysis = server.build_analysis("0700.HK")
        self.assertEqual([item["title"] for item in analysis["news"]], ["Tencent update"])
        self.assertEqual(analysis["name"], "Tencent Holdings Limited")


if __name__ == "__main__":
    unittest.main()
