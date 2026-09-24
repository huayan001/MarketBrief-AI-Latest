"""Manual, research-only US momentum scan backed by Longbridge OpenAPI."""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import momentum_strategy


class MomentumScannerError(RuntimeError):
    """An expected scanner configuration or provider failure."""


_scan_lock = Lock()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _client_id() -> str:
    configured = os.environ.get("LONGBRIDGE_OAUTH_CLIENT_ID", "").strip()
    if configured:
        return configured
    token_dir = Path.home() / ".longbridge" / "openapi" / "tokens"
    candidates = [
        item
        for item in token_dir.glob("*")
        if item.is_file() and not item.name.endswith((".backup", ".cli-backup", ".bak"))
    ]
    if len(candidates) == 1:
        return candidates[0].name
    raise MomentumScannerError(
        "未找到 Longbridge OAuth 配置，请设置 LONGBRIDGE_OAUTH_CLIENT_ID 并完成授权。"
    )


def _quote_context() -> Any:
    try:
        from longbridge.openapi import Config, OAuthBuilder, QuoteContext
    except ImportError as exc:
        raise MomentumScannerError("Longbridge SDK 尚未安装。") from exc

    region = os.environ.get("LONGBRIDGE_REGION", "cn").strip().lower() or "cn"
    os.environ.setdefault("LONGPORT_REGION", region)
    os.environ.setdefault("LONGBRIDGE_PRINT_QUOTE_PACKAGES", "false")

    def no_interactive_auth(url: str) -> None:
        raise MomentumScannerError(f"Longbridge 授权已失效，请重新授权：{url}")

    try:
        oauth = OAuthBuilder(_client_id()).build(no_interactive_auth)
        return QuoteContext(Config.from_oauth(oauth))
    except MomentumScannerError:
        raise
    except Exception as exc:
        raise MomentumScannerError(f"Longbridge 连接失败：{type(exc).__name__}") from exc


def _is_common_stock(symbol: str, english_name: str) -> bool:
    upper_symbol = str(symbol).upper()
    upper_name = str(english_name).upper()
    excluded = (
        "WARRANT", "PREFERRED", "PREF STK", "DEPOSITARY SH", "RIGHTS",
        " UNIT", " ETF", " ETN", "FUND", "TRUST",
    )
    return not any(token in upper_name for token in excluded) and not any(
        token in upper_symbol for token in ("-", ".U.US", ".W.US")
    )


def _candidate(
    raw: dict[str, Any], shares: float | None, closes: list[float], volumes: list[float]
) -> dict[str, Any]:
    macd = signal = histogram = None
    macd_open = macd_cross = False
    if len(closes) >= 35:
        fast = _ema(closes, 12)
        slow = _ema(closes, 26)
        macd_values = [a - b for a, b in zip(fast, slow)]
        signal_values = _ema(macd_values, 9)
        hist = [a - b for a, b in zip(macd_values, signal_values)]
        macd, signal, histogram = macd_values[-1], signal_values[-1], hist[-1]
        macd_open = macd > 0 and signal > 0 and histogram > 0
        macd_cross = any(value <= 0 for value in hist[-4:-1]) and histogram > 0

    volume_float_ratio = raw["volume"] / shares if raw["volume"] and shares else None
    shallow_pullback = False
    if len(closes) >= 4:
        recent_high = max(closes[-8:])
        pullback = (recent_high - closes[-1]) / recent_high if recent_high else 0
        shallow_pullback = 0 < pullback <= 0.08 and closes[-1] >= closes[-2]

    market_cap = raw["price"] * shares if shares else None
    quality = momentum_strategy.evaluate_candidate({**raw, "market_cap": market_cap})
    checks = {
        "change": raw["change_pct"] >= 10,
        "small_float": shares is not None and shares <= 10_000_000,
        "volume_float": volume_float_ratio is not None and volume_float_ratio >= 1,
        "macd_open": macd_open,
        "macd_cross": macd_cross,
        "pullback": shallow_pullback,
    }
    weights = (25, 20, 20, 20, 10, 5)
    score = sum(weight for weight, passed in zip(weights, checks.values()) if passed)
    breakout = len(closes) >= 8 and closes[-2] > max(closes[-8:-2])
    volume_confirmed = len(volumes) >= 22 and volumes[-2] >= sum(volumes[-22:-2]) / 20 * 1.5
    opening = momentum_strategy.opening_decision(
        datetime.now(timezone.utc), breakout=breakout,
        volume_confirmed=volume_confirmed, bar_closed=len(closes) >= 2,
    )
    return {
        **raw,
        "symbol": raw["symbol"].removesuffix(".US"),
        "float_shares": shares,
        "market_cap": market_cap,
        "volume_float_ratio": volume_float_ratio,
        **quality,
        "opening_decision": opening,
        "macd": macd,
        "macd_signal": signal,
        "macd_histogram": histogram,
        "checks": checks,
        "score": score,
        "alert": score >= 70 and macd_open and quality["eligible"] and opening["notify"],
    }


def scan_us_momentum(limit: int = 20) -> dict[str, Any]:
    """Run one full scan. This function is called only by the manual POST endpoint."""
    if not _scan_lock.acquire(blocking=False):
        raise MomentumScannerError("已有扫描正在运行，请稍后再试。")
    try:
        from longbridge.openapi import AdjustType, Market, Period, TradeSessions

        ctx = _quote_context()
        securities = ctx.security_list(Market.US)
        names = {
            item.symbol: (
                getattr(item, "name_cn", "")
                or getattr(item, "name_en", "")
                or item.symbol
            )
            for item in securities
            if _is_common_stock(item.symbol, getattr(item, "name_en", ""))
        }

        coarse: list[dict[str, Any]] = []
        symbols = list(names)
        for offset in range(0, len(symbols), 500):
            for quote in ctx.quote(symbols[offset : offset + 500]):
                price = _number(getattr(quote, "last_done", None))
                previous = _number(getattr(quote, "prev_close", None))
                volume = _number(getattr(quote, "volume", None))
                if not price or not previous or not volume:
                    continue
                change = (price - previous) / previous * 100
                if 3 <= price <= 30 and price * volume >= 10_000_000 and change >= 10:
                    symbol = str(getattr(quote, "symbol", ""))
                    coarse.append({
                        "symbol": symbol,
                        "name": names.get(symbol, symbol),
                        "price": price,
                        "change_pct": change,
                        "volume": volume,
                        "bid": _number(getattr(quote, "bid_price", None)),
                        "ask": _number(getattr(quote, "ask_price", None)),
                    })

        coarse.sort(key=lambda item: (item["change_pct"], item["volume"]), reverse=True)
        detail = coarse[: max(limit * 2, 30)]
        static_by_symbol = {
            str(getattr(item, "symbol", "")): item
            for item in (ctx.static_info([item["symbol"] for item in detail]) if detail else [])
        }

        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in detail:
            try:
                static = static_by_symbol.get(raw["symbol"])
                shares = (
                    _number(getattr(static, "circulating_shares", None))
                    or _number(getattr(static, "total_shares", None))
                    if static
                    else None
                )
                candles = ctx.candlesticks(
                    raw["symbol"], Period.Min_5, 100, AdjustType.NoAdjust, TradeSessions.All
                )
                closes = [value for bar in candles if (value := _number(getattr(bar, "close", None))) is not None]
                volumes = [value for bar in candles if (value := _number(getattr(bar, "volume", None))) is not None]
                candidates.append(_candidate(raw, shares, closes, volumes))
            except Exception as exc:
                warnings.append(f"{raw['symbol']}: {type(exc).__name__}")

        candidates.sort(key=lambda item: (item["score"], item["change_pct"]), reverse=True)
        candidates = candidates[: max(1, min(int(limit), 30))]
        for rank, item in enumerate(candidates, 1):
            item["rank"] = rank
        return {
            "provider": "Longbridge OpenAPI",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "universe_count": len(symbols),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "warnings": warnings[:8],
        }
    finally:
        _scan_lock.release()
