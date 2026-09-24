#!/usr/bin/env python3
"""Quant AI Workbench: local multi-market analysis server."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import auth
import brief_scheduler
import db
import entitlements
import market_cache
import momentum_alerts
import momentum_scanner
import robinhood_radar
import trade_review
import notifications
import security
import secret_store
import waffo_bridge


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_PORT = int(os.environ.get("QUANT_AI_PORT", "8787"))
PUBLIC_API_PATHS = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/ai-status"),
    ("GET", "/api/site-footer"),
    ("POST", "/api/auth/send-code"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}
BRIEF_CHANNELS = set(notifications.SUPPORTED_CHANNELS)


def mask_email(email: str) -> str:
    """Return a demo-safe email label without exposing the full local part."""
    value = str(email or "").strip()
    if "@" not in value:
        return "***"
    local, domain = value.rsplit("@", 1)
    visible = local[: min(3, max(1, len(local)))]
    return f"{visible}***@{domain}" if domain else f"{visible}***"


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in user.items() if key != "email"
    } | {
        "email": mask_email(str(user.get("email") or "")),
        "is_admin": is_site_admin(user),
    }


def is_site_admin(user: dict[str, Any] | None) -> bool:
    email = str((user or {}).get("email") or "").strip().lower()
    admin_source = os.environ.get("SITE_ADMIN_EMAILS", "").strip()
    if not admin_source:
        admin_source = os.environ.get("SMTP_USER", "").strip()
    configured = {
        item.strip().lower()
        for item in admin_source.split(",")
        if item.strip()
    }
    return bool(email and email in configured)


DEFAULT_SITE_FOOTER = {
    "description_zh": "股票市场信息、图表、公司研究与 AI 辅助摘要工具。仅供一般信息与自主研究。",
    "description_en": "Stock market information, charts, company research, and AI-assisted summaries for independent research.",
    "contact_email": "",
    "columns": [
        {"title": "研究", "links": [{"label": "美股", "url": "/us-stocks/"}, {"label": "公开报告", "url": "/reports/"}, {"label": "学习中心", "url": "/learn/"}]},
        {"title": "公司", "links": [{"label": "价格", "url": "/pricing/"}, {"label": "关于", "url": "/about/"}, {"label": "研究方法", "url": "/methodology/"}]},
        {"title": "法律", "links": [{"label": "隐私政策", "url": "/privacy.html"}, {"label": "服务条款", "url": "/terms.html"}, {"label": "退款与取消", "url": "/refund.html"}]},
    ],
    "socials": [],
}


def site_footer_payload() -> dict[str, Any]:
    return db.get_site_setting("footer") or DEFAULT_SITE_FOOTER


def normalize_site_footer(payload: dict[str, Any]) -> dict[str, Any]:
    def clean_text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def clean_url(value: Any) -> str:
        url = clean_text(value, 500)
        parsed = urllib.parse.urlparse(url)
        if url.startswith("/") or parsed.scheme in {"http", "https", "mailto"}:
            return url
        raise ValueError("链接必须是站内路径或 http/https/mailto 地址")

    columns = []
    for column in (payload.get("columns") or [])[:6]:
        if not isinstance(column, dict):
            continue
        links = []
        for link in (column.get("links") or [])[:12]:
            if not isinstance(link, dict):
                continue
            label = clean_text(link.get("label"), 80)
            url = clean_url(link.get("url"))
            if label and url:
                links.append({"label": label, "url": url})
        title = clean_text(column.get("title"), 80)
        if title:
            columns.append({"title": title, "links": links})
    socials = []
    for link in (payload.get("socials") or [])[:12]:
        if not isinstance(link, dict):
            continue
        label = clean_text(link.get("label"), 80)
        url = clean_url(link.get("url"))
        if label and url:
            socials.append({"label": label, "url": url})
    return {
        "description_zh": clean_text(payload.get("description_zh"), 500),
        "description_en": clean_text(payload.get("description_en"), 500),
        "contact_email": clean_text(payload.get("contact_email"), 255),
        "columns": columns,
        "socials": socials,
    }


def user_channel_configs(user_id: int) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    stored = db.get_notification_channel_configs(user_id)
    decrypted: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for channel, row in stored.items():
        summaries[channel] = {
            "configured": True,
            "label": row["masked_label"],
            "updated_at": row["updated_at"],
        }
        try:
            decrypted[channel] = secret_store.decrypt_config(row["encrypted"])
        except RuntimeError:
            summaries[channel]["configured"] = False
            summaries[channel]["error"] = "凭据需要重新配置"
    return decrypted, summaries


def normalize_channel_config(channel: str, payload: dict[str, Any]) -> tuple[dict[str, str], str]:
    if channel in {"wechat", "feishu"}:
        url = str(payload.get("webhook_url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Webhook 必须是有效的 HTTPS 地址")
        allowed_hosts = {
            "wechat": {"qyapi.weixin.qq.com"},
            "feishu": {"open.feishu.cn", "open.larksuite.com"},
        }
        if (parsed.hostname or "").lower() not in allowed_hosts[channel]:
            raise ValueError("Webhook 域名与所选渠道不匹配")
        return {"webhook_url": url}, f"{parsed.netloc}/…"
    if channel == "telegram":
        token = str(payload.get("bot_token") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        if len(token) < 20 or not chat_id:
            raise ValueError("请输入有效的 Telegram Bot Token 和 Chat ID")
        return {"bot_token": token, "chat_id": chat_id}, f"Chat …{chat_id[-4:]}"
    raise ValueError("该渠道不需要用户级配置")


def load_env_file() -> None:
    env_files = [ROOT / "api_keys.env", ROOT / ".env"]
    existing_files = [path for path in env_files if path.exists()]
    if not existing_files:
        return
    for env_path in existing_files:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def json_response(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in security.response_headers("application/json").items():
        handler.send_header(key, value)
    handler.send_header("X-Request-ID", request_id(handler))
    if headers:
        for key, value in headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def text_response(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    content_type: str = "text/html; charset=utf-8",
    status: int = 200,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in security.response_headers(content_type).items():
        handler.send_header(key, value)
    handler.send_header("X-Request-ID", request_id(handler))
    handler.end_headers()
    handler.wfile.write(body)


def request_id(handler: BaseHTTPRequestHandler) -> str:
    current = getattr(handler, "_request_id", "")
    if not current:
        current = uuid.uuid4().hex[:16]
        setattr(handler, "_request_id", current)
    return current


def internal_error(handler: BaseHTTPRequestHandler, exc: Exception) -> None:
    error_id = request_id(handler)
    print(f"[error:{error_id}] {type(exc).__name__}: {exc}")
    traceback.print_exc()
    message = "服务暂时不可用，请稍后重试"
    if not security.is_production():
        message = str(exc)
    json_response(handler, {"error": message, "request_id": error_id}, status=500)


def read_raw_body(handler: BaseHTTPRequestHandler, max_bytes: int = 1024 * 1024) -> bytes:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length < 0 or length > max_bytes:
        raise ValueError("Request body is too large")
    return handler.rfile.read(length) if length else b""


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw = read_raw_body(handler).decode("utf-8") or "{}"
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def require_user(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    user = auth.current_user(handler)
    if user:
        return user
    json_response(handler, {"error": "未登录"}, status=401)
    return None


def request_origin(handler: BaseHTTPRequestHandler) -> str:
    origin = str(handler.headers.get("Origin") or "").strip()
    parsed_origin = urllib.parse.urlparse(origin)
    if parsed_origin.scheme in {"http", "https"} and parsed_origin.netloc:
        return f"{parsed_origin.scheme}://{parsed_origin.netloc}"

    forwarded_proto = str(handler.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
    scheme = forwarded_proto if forwarded_proto in {"http", "https"} else "http"
    forwarded_host = str(handler.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    host = forwarded_host or str(handler.headers.get("Host") or "127.0.0.1").strip()
    if not host or any(char in host for char in "/\\\r\n"):
        raise ValueError("Invalid request host")
    return f"{scheme}://{host}"


def resumable_checkout(order: dict[str, Any]) -> bool:
    if order.get("status") != "pending":
        return False
    checkout_url = str(order.get("checkout_url") or "")
    if urllib.parse.urlparse(checkout_url).scheme != "https":
        return False
    raw_expiry = str(order.get("expires_at") or "")
    if not raw_expiry:
        return False
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    return expires_at > datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    safe = "".join(ch for ch in cleaned if ch.isalnum() or ch in ".-_=^")
    if not safe:
        raise ValueError("请输入股票、指数或币种代码")
    if safe.isdigit() and len(safe) == 6:
        if safe.startswith(("0", "2", "3")):
            return f"{safe}.SZ"
        if safe.startswith(("5", "6", "9")):
            return f"{safe}.SS"
    return safe[:32]


def detect_asset_type(symbol: str) -> str:
    if "-USD" in symbol or symbol.endswith("USDT") or symbol.endswith("BTC"):
        return "crypto"
    if symbol.startswith("^"):
        return "index"
    if "." in symbol:
        suffix = symbol.rsplit(".", 1)[-1]
        return {
            "HK": "hong_kong_stock",
            "SS": "china_a_share",
            "SZ": "china_a_share",
            "T": "japan_stock",
            "L": "uk_stock",
            "NS": "india_stock",
            "AX": "australia_stock",
            "TO": "canada_stock",
        }.get(suffix, "global_stock")
    return "us_stock"


def fetch_yahoo_chart(symbol: str, chart_range: str = "1y", interval: str = "1d") -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol, safe="")
    path = f"/v8/finance/chart/{encoded}?range={urllib.parse.quote(chart_range)}&interval={urllib.parse.quote(interval)}"
    cache_key = f"chart:{symbol}:{chart_range}:{interval}"
    try:
        payload = fetch_json_with_retries(["query1.finance.yahoo.com", "query2.finance.yahoo.com"], path)
        result = payload.get("chart", {}).get("result") or []
        error = payload.get("chart", {}).get("error")
        if error:
            raise RuntimeError(error.get("description") or "行情数据源返回错误")
        if not result:
            raise RuntimeError("没有找到该代码的行情数据")
        market_cache.put(cache_key, result[0])
        return result[0]
    except Exception:
        cached, _stale = market_cache.get(cache_key, 6 * 60 * 60)
        if cached is None:
            raise
        cached = dict(cached)
        cached["_marketbrief_data_mode"] = "stale_cache"
        return cached


def fetch_json_with_retries(hosts: list[str], path: str, attempts_per_host: int = 2) -> dict[str, Any]:
    errors = []
    for host in hosts:
        url = f"https://{host}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "QuantAIWorkbench/0.1"})
        for attempt in range(attempts_per_host):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                errors.append(f"{host}: HTTP {exc.code}")
                if exc.code in {404, 429}:
                    break
            except urllib.error.URLError as exc:
                errors.append(f"{host}: {exc.reason}")
            except Exception as exc:
                errors.append(f"{host}: {exc}")
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError("无法连接行情数据源：" + "；".join(errors[-4:]))


def fetch_yahoo_news(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(symbol, safe="")
    try:
        payload = fetch_json_with_retries(
            ["query1.finance.yahoo.com", "query2.finance.yahoo.com"],
            f"/v1/finance/search?q={encoded}&quotesCount=0&newsCount={limit}&enableFuzzyQuery=false",
            attempts_per_host=1,
        )
    except Exception:
        return []

    news_items = []
    for item in payload.get("news") or []:
        title = item.get("title") or ""
        if not title:
            continue
        published = item.get("providerPublishTime")
        news_items.append(
            {
                "title": title,
                "publisher": item.get("publisher") or "Yahoo Finance",
                "link": item.get("link") or "",
                "summary": item.get("summary") or item.get("snippet") or "",
                "published_at": (
                    datetime.fromtimestamp(published, timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if published
                    else ""
                ),
                "type": item.get("type") or "story",
            }
        )
    return news_items[:limit]


def fetch_yahoo_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    encoded = urllib.parse.quote(query.strip(), safe="")
    if not encoded:
        return []
    payload = fetch_json_with_retries(
        ["query1.finance.yahoo.com", "query2.finance.yahoo.com"],
        f"/v1/finance/search?q={encoded}&quotesCount={limit}&newsCount=0&enableFuzzyQuery=true",
        attempts_per_host=1,
    )
    results = []
    for item in payload.get("quotes") or []:
        symbol = item.get("symbol")
        if not symbol:
            continue
        results.append(
            {
                "symbol": symbol,
                "name": item.get("longname") or item.get("shortname") or item.get("name") or symbol,
                "exchange": item.get("exchDisp") or item.get("exchange") or "",
                "type": item.get("quoteType") or item.get("typeDisp") or "",
                "score": item.get("score"),
            }
        )
    return results[:limit]


def clean_series(raw: dict[str, Any]) -> dict[str, Any]:
    timestamps = raw.get("timestamp") or []
    quote = ((raw.get("indicators") or {}).get("quote") or [{}])[0]
    adjclose = ((raw.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    meta = raw.get("meta") or {}
    rows = []
    for idx, ts in enumerate(timestamps):
        close = value_at(adjclose, idx, value_at(quote.get("close"), idx))
        if close is None:
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                "open": value_at(quote.get("open"), idx),
                "high": value_at(quote.get("high"), idx),
                "low": value_at(quote.get("low"), idx),
                "close": close,
                "volume": value_at(quote.get("volume"), idx) or 0,
            }
        )
    if len(rows) < 2:
        raise RuntimeError("可用行情太少，无法计算涨跌和指标")
    return {"meta": meta, "rows": rows}


def value_at(values: Any, index: int, fallback: Any = None) -> Any:
    if not isinstance(values, list) or index >= len(values):
        return fallback
    return values[index]


def sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for idx, value in enumerate(values):
        total += value
        if idx >= window:
            total -= values[idx - window]
        out.append(total / window if idx >= window - 1 else None)
    return out


def ema(values: list[float], window: int) -> list[float | None]:
    if not values:
        return []
    alpha = 2 / (window + 1)
    out: list[float | None] = []
    current = values[0]
    for idx, value in enumerate(values):
        current = value if idx == 0 else alpha * value + (1 - alpha) * current
        out.append(current if idx >= window - 1 else None)
    return out


def rsi(values: list[float], window: int = 14) -> list[float | None]:
    out: list[float | None] = [None]
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(values)):
        change = values[idx] - values[idx - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
        if idx < window:
            out.append(None)
            continue
        avg_gain = sum(gains[-window:]) / window
        avg_loss = sum(losses[-window:]) / window
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100 - (100 / (1 + rs)))
    return out


def stddev(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for idx in range(len(values)):
        if idx < window - 1:
            out.append(None)
            continue
        chunk = values[idx - window + 1 : idx + 1]
        mean = sum(chunk) / window
        variance = sum((v - mean) ** 2 for v in chunk) / window
        out.append(math.sqrt(variance))
    return out


def atr(rows: list[dict[str, Any]], window: int = 14) -> list[float | None]:
    true_ranges = []
    for idx, row in enumerate(rows):
        high = float(row["high"] or row["close"])
        low = float(row["low"] or row["close"])
        prev_close = float(rows[idx - 1]["close"]) if idx else float(row["close"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sma(true_ranges, window)


def pct_change(current: float, previous: float | None) -> float | None:
    if previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def last_non_null(values: list[Any]) -> Any:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def build_analysis(symbol: str, include_news: bool = True) -> dict[str, Any]:
    raw = fetch_yahoo_chart(symbol)
    news = fetch_yahoo_news(symbol) if include_news else []
    cleaned = clean_series(raw)
    rows = cleaned["rows"]
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row["volume"] or 0) for row in rows]
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [
        (a - b) if a is not None and b is not None else None for a, b in zip(ema12, ema26)
    ]
    macd_values = [v if v is not None else 0.0 for v in macd_line]
    signal_line = ema(macd_values, 9)
    histogram = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, signal_line)
    ]
    rsi14 = rsi(closes, 14)
    std20 = stddev(closes, 20)
    boll_mid = ma20
    boll_upper = [
        (m + 2 * s) if m is not None and s is not None else None for m, s in zip(boll_mid, std20)
    ]
    boll_lower = [
        (m - 2 * s) if m is not None and s is not None else None for m, s in zip(boll_mid, std20)
    ]
    atr14 = atr(rows, 14)
    vol20 = sma(volumes, 20)
    latest = rows[-1]
    close = float(latest["close"])
    previous_close = float(rows[-2]["close"]) if len(rows) >= 2 else close
    change_pct = pct_change(close, previous_close) or 0.0
    perf_20 = pct_change(close, closes[-21] if len(closes) > 21 else None)
    perf_60 = pct_change(close, closes[-61] if len(closes) > 61 else None)
    perf_120 = pct_change(close, closes[-121] if len(closes) > 121 else None)
    latest_ma20 = last_non_null(ma20)
    latest_ma60 = last_non_null(ma60)
    latest_rsi = last_non_null(rsi14)
    latest_macd = last_non_null(macd_line)
    latest_hist = last_non_null(histogram)
    latest_atr = last_non_null(atr14)
    latest_vol20 = last_non_null(vol20)
    volume_ratio = (latest["volume"] / latest_vol20) if latest_vol20 else None
    volatility = (
        statistics_like_daily_volatility(closes[-60:]) if len(closes) >= 60 else None
    )
    scores = score_snapshot(
        close=close,
        ma20=latest_ma20,
        ma60=latest_ma60,
        rsi_value=latest_rsi,
        macd_hist=latest_hist,
        volume_ratio=volume_ratio,
        volatility=volatility,
        perf_20=perf_20,
    )
    signal = build_trade_signal(
        close=close,
        ma20=latest_ma20,
        ma60=latest_ma60,
        rsi_value=latest_rsi,
        macd_hist=latest_hist,
        boll_upper=last_non_null(boll_upper),
        boll_lower=last_non_null(boll_lower),
        atr_value=latest_atr,
        volume_ratio=volume_ratio,
        scores=scores,
    )
    timeframe_signals = build_timeframe_signals(
        closes=closes,
        ma20=ma20,
        ma60=ma60,
        rsi14=rsi14,
        histogram=histogram,
        volume_ratio=volume_ratio,
        risk_score=scores["risk_control"],
    )
    enriched_rows = []
    for idx, row in enumerate(rows[-180:]):
        source_idx = len(rows) - len(rows[-180:]) + idx
        enriched_rows.append(
            {
                **row,
                "ma20": ma20[source_idx],
                "ma60": ma60[source_idx],
                "rsi": rsi14[source_idx],
                "macd": macd_line[source_idx],
                "macd_signal": signal_line[source_idx],
                "macd_hist": histogram[source_idx],
                "boll_upper": boll_upper[source_idx],
                "boll_mid": boll_mid[source_idx],
                "boll_lower": boll_lower[source_idx],
                "atr": atr14[source_idx],
            }
        )
    meta = cleaned["meta"]
    data_health = build_data_health(symbol, meta, rows)
    warnings = build_data_warnings(data_health)
    return {
        "symbol": symbol,
        "asset_type": detect_asset_type(symbol),
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency") or "",
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or "",
        "data_warnings": warnings,
        "data_health": data_health,
        "quote": {
            "date": latest["date"],
            "price": close,
            "change_pct": change_pct,
            "volume": latest["volume"],
            "performance": {"20d": perf_20, "60d": perf_60, "120d": perf_120},
        },
        "latest_indicators": {
            "ma20": latest_ma20,
            "ma60": latest_ma60,
            "rsi14": latest_rsi,
            "macd": latest_macd,
            "macd_hist": latest_hist,
            "boll_upper": last_non_null(boll_upper),
            "boll_lower": last_non_null(boll_lower),
            "atr14": latest_atr,
            "volume_ratio": volume_ratio,
            "volatility_60d_annualized": volatility,
        },
        "signal": signal,
        "timeframe_signals": timeframe_signals,
        "news": news,
        "scores": scores,
        "series": enriched_rows,
    }


def build_scan_item(symbol: str) -> dict[str, Any]:
    analysis = build_analysis(symbol, include_news=False)
    signal = analysis.get("signal") or {}
    quote = analysis["quote"]
    scores = analysis["scores"]
    warnings = analysis.get("data_warnings") or []
    health = analysis.get("data_health") or {}
    return {
        "symbol": analysis["symbol"],
        "name": analysis.get("name", analysis["symbol"]),
        "asset_type": analysis["asset_type"],
        "exchange": analysis.get("exchange", ""),
        "date": quote.get("date"),
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "volume": quote.get("volume"),
        "overall": scores.get("overall"),
        "trend": scores.get("trend"),
        "momentum": scores.get("momentum"),
        "risk_control": scores.get("risk_control"),
        "signal": {
            "action": signal.get("action"),
            "label": signal.get("label"),
            "tone": signal.get("tone"),
            "confidence": signal.get("confidence"),
            "score": signal.get("score"),
        },
        "warnings": warnings,
        "data_status": health.get("status"),
        "data_status_label": health.get("status_label"),
    }


def scan_symbols(symbols: list[str]) -> list[dict[str, Any]]:
    results = []
    seen = set()
    for raw_symbol in symbols[:30]:
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError as exc:
            results.append({"symbol": raw_symbol, "error": str(exc)})
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            results.append(build_scan_item(symbol))
        except Exception as exc:
            results.append({"symbol": symbol, "error": str(exc)})
    return results


def _is_tokenized_equity_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in ("tokenized", "tokenised", "prestocks", "xstock"))


def crypto_quote_is_expected(symbol: str, meta: dict[str, Any]) -> bool:
    """True when a Yahoo ``-USD`` quote is the cryptocurrency the user asked for.

    ``BTC-USD`` resolves to instrument type CRYPTOCURRENCY. That is a correct
    mapping, not a US stock ticker with a stray ``-USD`` suffix. Tokenized
    equities (PreStocks, xStock, and similar wrappers) keep the mapping warning.
    """
    name = str(meta.get("longName") or meta.get("shortName") or "")
    if _is_tokenized_equity_name(name):
        return False
    instrument = str(meta.get("instrumentType") or meta.get("quoteType") or "").strip().upper()
    if instrument in {"CRYPTOCURRENCY", "CRYPTO"}:
        return True
    if instrument:
        return False
    return detect_asset_type(symbol) == "crypto"


def build_data_health(symbol: str, meta: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = []
    exchange = meta.get("exchangeName") or meta.get("fullExchangeName") or ""
    name = meta.get("longName") or meta.get("shortName") or symbol
    suggestions = []
    if symbol.endswith("-USD") and exchange == "CCC" and not crypto_quote_is_expected(symbol, meta):
        base_symbol = symbol.removesuffix("-USD")
        suggestions.append({"symbol": base_symbol, "reason": "去掉 -USD 后按股票代码重试"})
        warnings.append(
            f"{symbol} 被行情源识别为加密资产/代币：{name}。如果要查询美股股票，请去掉 -USD，例如输入 {base_symbol}。"
        )
    if len(rows) < 30:
        warnings.append("该标的历史行情少于 30 根，K 线会展示已有数据，MA20/MA60/RSI/MACD 等指标可能为空或不稳定。")
    age_days = None
    try:
        last_date = datetime.strptime(rows[-1]["date"], "%Y-%m-%d").date()
        age_days = (datetime.now(timezone.utc).date() - last_date).days
        if age_days > 7:
            warnings.append(f"行情源最后一根 K 线停留在 {rows[-1]['date']}，可能是代码停牌、退市、映射错误或数据源未更新。")
    except Exception:
        pass
    status = "ok"
    status_label = "数据正常"
    if any("被行情源识别为加密资产/代币" in item for item in warnings):
        status = "mismatch"
        status_label = "疑似代码映射错误"
    elif age_days is not None and age_days > 7:
        status = "stale"
        status_label = "数据可能过期"
    elif len(rows) < 30:
        status = "limited"
        status_label = "历史数据较短"
    return {
        "provider": "Yahoo Finance",
        "symbol": symbol,
        "source_name": name,
        "exchange": exchange,
        "instrument_type": meta.get("instrumentType") or "",
        "currency": meta.get("currency") or "",
        "first_date": rows[0]["date"] if rows else "",
        "last_date": rows[-1]["date"] if rows else "",
        "bar_count": len(rows),
        "age_days": age_days,
        "status": status,
        "status_label": status_label,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def build_data_warnings(health: dict[str, Any]) -> list[str]:
    return list(health.get("warnings") or [])


def public_payment_order(order: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop stale test-environment unknown orders from subscription-facing reads.

    A checkout that never received a provider result is stored as ``unknown``.
    That is not an active subscription, and it must not override owner or Pro
    grants shown by the entitlement snapshot.
    """
    if not order:
        return None
    environment = str(order.get("environment") or "").strip().lower()
    status = str(order.get("status") or "").strip().lower()
    if environment == "test" and status == "unknown":
        return None
    return order


def respond_analysis(handler: BaseHTTPRequestHandler, user: dict[str, Any], symbol: str) -> None:
    usage = entitlements.consume_or_raise(
        int(user["id"]),
        "analysis_daily",
    )
    try:
        analysis = build_analysis(symbol)
    except Exception:
        entitlements.refund(
            int(user["id"]),
            "analysis_daily",
            period_key=usage.get("period_key"),
        )
        raise
    analysis["entitlement_usage"] = usage
    json_response(handler, analysis)


def statistics_like_daily_volatility(values: list[float]) -> float:
    returns = []
    for idx in range(1, len(values)):
        if values[idx - 1] != 0:
            returns.append(values[idx] / values[idx - 1] - 1)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100


def score_snapshot(**kwargs: Any) -> dict[str, float]:
    close = kwargs["close"]
    ma20 = kwargs["ma20"]
    ma60 = kwargs["ma60"]
    rsi_value = kwargs["rsi_value"]
    macd_hist = kwargs["macd_hist"]
    volume_ratio = kwargs["volume_ratio"]
    volatility = kwargs["volatility"]
    perf_20 = kwargs["perf_20"]
    trend = 50
    if ma20:
        trend += 20 if close > ma20 else -20
    if ma60:
        trend += 20 if close > ma60 else -20
    momentum = 50 + clamp((perf_20 or 0) * 2, -35, 35)
    if macd_hist is not None:
        momentum += 10 if macd_hist > 0 else -10
    if rsi_value is not None:
        momentum -= max(0, rsi_value - 75) * 0.8
        momentum += max(0, 35 - rsi_value) * 0.5
    volume_score = 50
    if volume_ratio is not None:
        volume_score += clamp((volume_ratio - 1) * 35, -25, 30)
    risk = 75
    if volatility is not None:
        risk -= clamp((volatility - 25) * 1.2, -15, 45)
    if rsi_value is not None and (rsi_value > 80 or rsi_value < 20):
        risk -= 15
    return {
        "trend": round(clamp(trend), 1),
        "momentum": round(clamp(momentum), 1),
        "volume": round(clamp(volume_score), 1),
        "risk_control": round(clamp(risk), 1),
        "overall": round(clamp((trend + momentum + volume_score + risk) / 4), 1),
    }


def build_trade_signal(**kwargs: Any) -> dict[str, Any]:
    close = kwargs["close"]
    ma20 = kwargs["ma20"]
    ma60 = kwargs["ma60"]
    rsi_value = kwargs["rsi_value"]
    macd_hist = kwargs["macd_hist"]
    boll_upper = kwargs["boll_upper"]
    boll_lower = kwargs["boll_lower"]
    atr_value = kwargs["atr_value"]
    volume_ratio = kwargs["volume_ratio"]
    scores = kwargs["scores"]
    points = 0
    reasons: list[str] = []
    cautions: list[str] = []

    if ma20 and close > ma20:
        points += 18
        reasons.append("价格站上 MA20，短线趋势偏强。")
    elif ma20:
        points -= 16
        cautions.append("价格低于 MA20，短线趋势仍弱。")

    if ma60 and close > ma60:
        points += 16
        reasons.append("价格站上 MA60，中期结构尚可。")
    elif ma60:
        points -= 14
        cautions.append("价格低于 MA60，中期结构偏谨慎。")

    if macd_hist is not None and macd_hist > 0:
        points += 14
        reasons.append("MACD 柱体为正，动量改善。")
    elif macd_hist is not None:
        points -= 12
        cautions.append("MACD 柱体为负，动量尚未转强。")

    if rsi_value is not None:
        if 45 <= rsi_value <= 68:
            points += 10
            reasons.append("RSI 位于健康区间，未明显过热。")
        elif rsi_value > 75:
            points -= 20
            cautions.append("RSI 偏高，短线有过热回撤风险。")
        elif rsi_value < 35:
            points -= 6
            cautions.append("RSI 偏弱，需等待企稳确认。")

    if volume_ratio is not None:
        if volume_ratio >= 1.25:
            points += 8
            reasons.append("量能高于 20 日均量，关注度提升。")
        elif volume_ratio < 0.65:
            points -= 6
            cautions.append("量能偏低，信号确认度不足。")

    if scores["risk_control"] < 40:
        points -= 18
        cautions.append("风险评分偏低，不适合激进跟进。")
    elif scores["risk_control"] > 65:
        points += 8
        reasons.append("风险评分较稳，波动压力可控。")

    confidence = round(clamp(50 + points * 0.7, 5, 95), 1)
    if points >= 30:
        action = "buy_watch"
        label = "买入观察"
        tone = "bullish"
    elif points >= 8:
        action = "hold_watch"
        label = "持有观察"
        tone = "neutral"
    elif points <= -25:
        action = "avoid_or_sell"
        label = "卖出/回避"
        tone = "bearish"
    else:
        action = "wait"
        label = "等待确认"
        tone = "neutral"

    support = [v for v in [ma20, boll_lower, ma60] if v]
    resistance = [v for v in [boll_upper, close + (atr_value or close * 0.03), close * 1.03] if v]
    stop_reference = None
    if atr_value:
        stop_reference = close - 1.5 * atr_value
    elif ma20:
        stop_reference = ma20 * 0.98

    return {
        "action": action,
        "label": label,
        "tone": tone,
        "confidence": confidence,
        "score": points,
        "reasons": reasons[:4],
        "cautions": cautions[:4],
        "levels": {
            "support": [round(v, 4) for v in support[:3]],
            "resistance": [round(v, 4) for v in resistance[:3]],
            "stop_reference": round(stop_reference, 4) if stop_reference else None,
        },
        "disclaimer": "信号仅用于研究观察，不构成买卖建议或交易指令。",
    }


def build_timeframe_signals(**kwargs: Any) -> list[dict[str, Any]]:
    closes = kwargs["closes"]
    ma20 = kwargs["ma20"]
    ma60 = kwargs["ma60"]
    rsi14 = kwargs["rsi14"]
    histogram = kwargs["histogram"]
    volume_ratio = kwargs["volume_ratio"]
    risk_score = kwargs["risk_score"]
    close = closes[-1]
    frames = [
        ("短线", "1-5 个交易日", 5, last_non_null(ma20), "看价格能否延续 MA20 上方强度。"),
        ("波段", "2-4 周", 20, last_non_null(ma20), "看 20 日趋势和 MACD 动量是否同向。"),
        ("中线", "1-3 个月", 60, last_non_null(ma60), "看价格是否保持在 MA60 上方并控制回撤。"),
    ]
    latest_rsi = last_non_null(rsi14)
    latest_hist = last_non_null(histogram)
    signals = []
    for name, horizon, lookback, anchor, focus in frames:
        past = closes[-lookback - 1] if len(closes) > lookback else None
        perf = pct_change(close, past)
        points = 0
        notes = []
        if anchor:
            if close > anchor:
                points += 22
                notes.append("价格在关键均线上方")
            else:
                points -= 22
                notes.append("价格低于关键均线")
        if perf is not None:
            if perf > 3:
                points += 16
                notes.append(f"{lookback} 日表现偏强")
            elif perf < -3:
                points -= 16
                notes.append(f"{lookback} 日表现偏弱")
        if latest_hist is not None:
            points += 10 if latest_hist > 0 else -10
        if latest_rsi is not None:
            if latest_rsi > 75:
                points -= 14
                notes.append("RSI 偏热")
            elif latest_rsi < 35:
                points -= 8
                notes.append("RSI 偏弱")
        if volume_ratio is not None and volume_ratio >= 1.2:
            points += 6
        if risk_score < 40:
            points -= 12
            notes.append("风险评分偏低")
        label = "等待确认"
        tone = "neutral"
        if points >= 24:
            label = "买入观察"
            tone = "bullish"
        elif points <= -22:
            label = "卖出/回避"
            tone = "bearish"
        elif points >= 6:
            label = "持有观察"
        signals.append(
            {
                "name": name,
                "horizon": horizon,
                "label": label,
                "tone": tone,
                "confidence": round(clamp(50 + points * 0.8, 5, 95), 1),
                "performance": perf,
                "focus": focus,
                "notes": notes[:3],
            }
        )
    return signals


def classify_key_levels(
    price: float,
    support_candidates: list[Any],
    resistance_candidates: list[Any],
) -> tuple[list[float], list[float]]:
    """Return truthful, de-duplicated levels on the correct side of price."""

    def numbers(values: list[Any]) -> list[float]:
        result: list[float] = []
        for value in values:
            try:
                number = round(float(value), 4)
            except (TypeError, ValueError):
                continue
            if number > 0 and all(abs(number - existing) > max(price * 0.0005, 0.0001) for existing in result):
                result.append(number)
        return result

    supports = sorted((value for value in numbers(support_candidates) if value < price), reverse=True)
    resistances = sorted(value for value in numbers(resistance_candidates) if value > price)
    return supports[:3], resistances[:3]


def local_report(analysis: dict[str, Any]) -> dict[str, Any]:
    scores = analysis["scores"]
    indicators = analysis["latest_indicators"]
    price = analysis["quote"]["price"]
    signal = analysis.get("signal") or {}
    rsi_value = indicators.get("rsi14")
    ma20 = indicators.get("ma20")
    ma60 = indicators.get("ma60")
    stance = "neutral"
    if scores["overall"] >= 68 and scores["risk_control"] >= 45:
        stance = "bullish"
    elif scores["overall"] <= 42 or scores["risk_control"] <= 35:
        stance = "bearish"
    opportunities = []
    risks = []
    if ma20 and price > ma20:
        opportunities.append("价格位于 MA20 上方，短期趋势保持相对强势。")
    else:
        risks.append("价格未能站稳 MA20，短线趋势仍需确认。")
    if ma60 and price > ma60:
        opportunities.append("价格位于 MA60 上方，中期趋势结构较健康。")
    else:
        risks.append("价格低于或接近 MA60，中期趋势可能偏弱。")
    if rsi_value is not None:
        if rsi_value > 75:
            risks.append("RSI 偏高，短线存在过热或回撤风险。")
        elif rsi_value < 35:
            opportunities.append("RSI 偏低，若价格企稳可能出现修复机会。")
    if indicators.get("volume_ratio") and indicators["volume_ratio"] > 1.5:
        opportunities.append("成交量显著高于 20 日均量，资金关注度提升。")
    if indicators.get("volatility_60d_annualized") and indicators["volatility_60d_annualized"] > 45:
        risks.append("近 60 日年化波动率较高，仓位和止损需要更保守。")
    news = analysis.get("news") or []
    if news:
        latest_titles = "；".join(item.get("title", "") for item in news[:3] if item.get("title"))
        opportunities.append(f"近期资讯已纳入观察，最新主题包括：{latest_titles}。")
    else:
        risks.append("当前新闻源没有返回相关资讯，报告主要依赖价格和技术指标。")
    support, resistance = classify_key_levels(
        price,
        [ma20, indicators.get("boll_lower"), ma60, price * 0.97],
        [indicators.get("boll_upper"), ma20, ma60, price * 1.03],
    )
    stance_text = {"bullish": "偏多", "neutral": "中性", "bearish": "偏空"}[stance]
    catalysts = [
        f"关注事件：{item.get('title', '')}"
        for item in news[:3]
        if item.get("title")
    ]
    checklist = [
        "价格是否站稳 MA20。",
        "MACD 柱体是否连续改善。",
        "成交量是否高于 20 日均量。",
        "最新新闻是否改变当前风险判断。",
    ]
    return {
        "source": "local_rules",
        "stance": stance,
        "summary": f"{analysis['symbol']} 当前综合评分 {scores['overall']}，观点为{stance_text}。",
        "opportunities": opportunities[:4] or ["当前没有明显的高确定性机会，建议等待更清晰的趋势或量能信号。"],
        "risks": risks[:4] or ["主要风险来自行情突发变化、数据延迟和模型判断不确定性。"],
        "key_levels": {"support": support, "resistance": resistance},
        "catalysts": catalysts or ["暂无明确事件催化，继续观察价格与量能确认。"],
        "action_checklist": checklist,
        "trade_signal": signal,
        "watch_plan": [
            "观察价格能否持续站稳 MA20。",
            "观察 MACD 柱体是否继续改善。",
            "跟踪后续新闻是否确认或反转当前价格信号。",
            "若放量突破近期压力位，再提高关注级别。",
        ],
        "news_brief": [
            {
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "published_at": item.get("published_at", ""),
            }
            for item in news[:5]
        ],
        "disclaimer": "仅供研究和信息分析，不构成投资建议或交易指令。",
    }


def normalize_brief_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled"))
    delivery_time = str(payload.get("delivery_time") or "18:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", delivery_time):
        raise ValueError("推送时间必须使用 HH:MM 格式")
    timezone_name = str(payload.get("timezone") or "Asia/Shanghai").strip()[:64]
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError("无效的时区") from exc
    channels = list(
        dict.fromkeys(
            str(item).strip().lower()
            for item in (payload.get("channels") or ["email"])
            if str(item).strip().lower() in BRIEF_CHANNELS
        )
    )
    if enabled and not channels:
        raise ValueError("请至少选择一个通知渠道")
    confidence = clamp(float(payload.get("min_confidence", 40)), 0, 100)
    return {
        "enabled": enabled,
        "delivery_time": delivery_time,
        "timezone": timezone_name,
        "channels": channels,
        "only_changes": bool(payload.get("only_changes", True)),
        "min_confidence": round(confidence, 1),
    }


def format_daily_brief(local_date: str, items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    lines = [f"MarketBrief AI 每日简报 · {local_date}", ""]
    details = []
    for item in items:
        report = item["report"]
        scan = item["scan"]
        signal = scan.get("signal") or {}
        lines.extend(
            [
                f"【{scan['symbol']} · {signal.get('label') or '等待确认'}】",
                report.get("summary", ""),
                "风险：" + "；".join((report.get("risks") or [])[:2]),
                "催化：" + "；".join((report.get("catalysts") or [])[:2]),
                "检查：" + "；".join((report.get("action_checklist") or [])[:3]),
                "",
            ]
        )
        details.append({"scan": scan, "report": report})
    lines.append("仅供研究和信息分析，不构成投资建议或交易指令。")
    return "\n".join(lines), {"date": local_date, "items": details}


def run_daily_brief(preferences: dict[str, Any]) -> dict[str, Any]:
    user_id = int(preferences["user_id"])
    symbols = db.get_watch_symbols(user_id)
    job_id = db.create_analysis_job(user_id, preferences["local_date"], symbols)
    if job_id is None:
        return {"status": "already_run", "local_date": preferences["local_date"]}
    if not symbols:
        db.fail_analysis_job(job_id, "关注列表为空")
        return {"status": "failed", "job_id": job_id, "error": "关注列表为空"}
    try:
        previous = db.get_signal_snapshots(user_id)
        scanned = scan_symbols(symbols)
        selected = []
        checked_at = datetime.now(timezone.utc).isoformat()
        for item in scanned:
            if item.get("error") or not item.get("signal"):
                continue
            signal = item["signal"]
            confidence = float(signal.get("confidence") or 0)
            old = previous.get(item["symbol"])
            changed = not old or old.get("action") != signal.get("action")
            snapshot = {
                "action": signal.get("action"),
                "label": signal.get("label"),
                "tone": signal.get("tone"),
                "checked_at": checked_at,
            }
            db.upsert_signal_snapshot(user_id, item["symbol"], snapshot)
            if old and changed:
                db.add_signal_event(
                    user_id,
                    item["symbol"],
                    {
                        "symbol": item["symbol"],
                        "name": item.get("name", ""),
                        "previous_label": old.get("label", "未知"),
                        "current_label": signal.get("label", "未知"),
                        "tone": signal.get("tone", "neutral"),
                        "price": item.get("price"),
                        "overall": item.get("overall"),
                        "confidence": confidence,
                        "quote_date": item.get("date"),
                        "checked_at": checked_at,
                    },
                )
            if confidence < preferences["min_confidence"]:
                continue
            if preferences["only_changes"] and old and not changed:
                continue
            selected.append(item)
        if not selected:
            brief_payload = {"date": preferences["local_date"], "items": [], "message": "今日暂无达到条件的重要变化"}
            db.complete_analysis_job(job_id, brief_payload)
            return {"status": "completed", "job_id": job_id, "sent": 0, "brief": brief_payload}
        detailed = []
        for item in selected[:10]:
            analysis = build_analysis(item["symbol"])
            report, _meta = call_ai_report(analysis)
            detailed.append({"scan": item, "report": report or local_report(analysis)})
        text, brief_payload = format_daily_brief(preferences["local_date"], detailed)
        subject = f"MarketBrief AI 每日简报 · {preferences['local_date']}"
        sent = 0
        channel_configs, _summaries = user_channel_configs(user_id)
        for channel in preferences["channels"]:
            try:
                attempts = notifications.send_with_retry(
                    channel, preferences["email"], subject, text,
                    config=channel_configs.get(channel),
                )
                db.record_notification_delivery(
                    job_id, user_id, channel, status="sent", attempt_count=attempts
                )
                sent += 1
            except Exception as exc:
                db.record_notification_delivery(
                    job_id, user_id, channel, status="failed", error=str(exc), attempt_count=3
                )
        db.complete_analysis_job(job_id, brief_payload)
        return {"status": "completed", "job_id": job_id, "sent": sent, "brief": brief_payload}
    except Exception as exc:
        db.fail_analysis_job(job_id, str(exc))
        raise


def get_ai_config(include_secret: bool = False) -> dict[str, Any]:
    providers = [
        (
            "deepseek",
            "DEEPSEEK_API_KEY",
            os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        ),
        (
            "openrouter",
            "OPENROUTER_API_KEY",
            os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
        ),
        (
            "openai",
            "OPENAI_API_KEY",
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        ),
    ]
    for provider, key_name, base_url, model in providers:
        api_key = os.environ.get(key_name)
        if not api_key:
            continue
        config = {
            "configured": True,
            "provider": provider,
            "key_name": key_name,
            "base_url": base_url.rstrip("/"),
            "model": model,
        }
        if include_secret:
            config["api_key"] = api_key
        return config
    return {
        "configured": False,
        "provider": "local_rules",
        "base_url": "",
        "model": "",
    }


def public_ai_config() -> dict[str, Any]:
    config = get_ai_config(False)
    return {
        "configured": config["configured"],
        "provider": config["provider"],
        "base_url": config["base_url"],
        "model": config["model"],
    }


def format_ai_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail
        return f"HTTP {exc.code}: {str(message)[:240]}"
    if isinstance(exc, urllib.error.URLError):
        return f"连接失败: {exc.reason}"
    return str(exc)[:240]


def call_ai_report(analysis: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    config = get_ai_config(True)
    meta = {
        "configured": config["configured"],
        "used": False,
        "provider": config["provider"],
        "model": config["model"],
    }
    if not config["configured"]:
        meta["error"] = "未配置 API Key，已使用本地规则生成报告。"
        return None, meta
    base_url = config["base_url"]
    model = config["model"]
    prompt = {
        "symbol": analysis["symbol"],
        "asset_type": analysis["asset_type"],
        "quote": analysis["quote"],
        "latest_indicators": analysis["latest_indicators"],
        "scores": analysis["scores"],
        "signal": analysis.get("signal", {}),
        "timeframe_signals": analysis.get("timeframe_signals", []),
        "news": analysis.get("news", [])[:8],
    }
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是谨慎的量化研究助理。请输出严格 JSON，字段包含 stance, summary, "
                    "opportunities, risks, catalysts, action_checklist, key_levels, watch_plan, disclaimer。"
                    "opportunities、risks、catalysts、action_checklist、watch_plan 必须是字符串数组；"
                    "key_levels 必须包含 support 和 resistance 两个数组。不要给自动下单建议。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": "QuantAIWorkbench/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(extract_json(content))
        parsed["source"] = "ai"
        parsed["provider"] = config["provider"]
        parsed["model"] = config["model"]
        report = normalize_report(parsed, analysis)
        meta["used"] = True
        return report, meta
    except Exception as exc:
        meta["error"] = format_ai_error(exc)
        return None, meta


def coerce_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = (
                    item.get("text")
                    or item.get("title")
                    or item.get("summary")
                    or json.dumps(item, ensure_ascii=False)
                )
            else:
                text = str(item)
            if text:
                result.append(text)
        return result
    if isinstance(value, dict):
        return [
            f"{key}: {val}" if not isinstance(val, (dict, list)) else f"{key}: {json.dumps(val, ensure_ascii=False)}"
            for key, val in value.items()
        ]
    text = str(value).strip()
    return [text] if text else []


def coerce_number_list(value: Any) -> list[float | str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result: list[float | str] = []
    for item in value:
        if item in (None, ""):
            continue
        try:
            result.append(round(float(item), 4))
        except (TypeError, ValueError):
            result.append(str(item))
    return result


def normalize_report(report: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    fallback = local_report(analysis)
    stance = str(report.get("stance") or fallback["stance"]).lower()
    if stance not in {"bullish", "neutral", "bearish"}:
        stance = fallback["stance"]
    key_levels = report.get("key_levels") if isinstance(report.get("key_levels"), dict) else {}
    raw_support = coerce_number_list(key_levels.get("support"))
    raw_resistance = coerce_number_list(key_levels.get("resistance"))
    numeric_support = [value for value in raw_support if isinstance(value, (int, float))]
    numeric_resistance = [value for value in raw_resistance if isinstance(value, (int, float))]
    support, resistance = classify_key_levels(
        float(analysis["quote"]["price"]),
        numeric_support or fallback["key_levels"]["support"],
        numeric_resistance or fallback["key_levels"]["resistance"],
    )
    normalized = {
        "source": report.get("source", "ai"),
        "provider": report.get("provider", ""),
        "model": report.get("model", ""),
        "stance": stance,
        "summary": str(report.get("summary") or fallback["summary"]),
        "opportunities": coerce_text_list(report.get("opportunities")) or fallback["opportunities"],
        "risks": coerce_text_list(report.get("risks")) or fallback["risks"],
        "catalysts": coerce_text_list(report.get("catalysts")) or fallback["catalysts"],
        "action_checklist": coerce_text_list(report.get("action_checklist")) or fallback["action_checklist"],
        "key_levels": {
            "support": support,
            "resistance": resistance,
        },
        "watch_plan": coerce_text_list(report.get("watch_plan")) or fallback["watch_plan"],
        "trade_signal": report.get("trade_signal") if isinstance(report.get("trade_signal"), dict) else fallback.get("trade_signal"),
        "news_brief": report.get("news_brief") if isinstance(report.get("news_brief"), list) else fallback.get("news_brief", []),
        "disclaimer": str(report.get("disclaimer") or fallback["disclaimer"]),
    }
    return normalized


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketBriefAI/0.2"

    def log_message(self, fmt: str, *args: Any) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                if ("GET", path) not in PUBLIC_API_PATHS:
                    user = require_user(self)
                    if not user:
                        return
                else:
                    user = auth.current_user(self)

                if path == "/api/auth/me":
                    if not user:
                        json_response(self, {"error": "未登录"}, status=401)
                        return
                    json_response(
                        self,
                        {
                            "user": public_user(user),
                            "entitlement": entitlements.entitlement_snapshot(int(user["id"])),
                        },
                    )
                    return
                if path == "/api/health":
                    database_ok = True
                    try:
                        with db.get_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT 1")
                                cur.fetchone()
                    except Exception:
                        database_ok = False
                    json_response(
                        self,
                        {
                            "status": "ok" if database_ok else "degraded",
                            "database": database_ok,
                            "scheduler_mode": os.environ.get("BRIEF_SCHEDULER_MODE", "embedded"),
                            "channels": notifications.channel_status(),
                        },
                        status=200 if database_ok else 503,
                    )
                    return
                if path == "/api/site-footer":
                    json_response(self, {"footer": site_footer_payload(), "is_admin": is_site_admin(user)})
                    return
                if path == "/api/payments/status":
                    query = urllib.parse.parse_qs(parsed.query)
                    external_id = str((query.get("order") or [""])[0]).strip()[:64] or None
                    order = public_payment_order(db.get_payment_order(int(user["id"]), external_id))
                    json_response(self, {"order": order})
                    return
                if path == "/api/analyze":
                    query = urllib.parse.parse_qs(parsed.query)
                    symbol = normalize_symbol((query.get("symbol") or [""])[0])
                    respond_analysis(self, user, symbol)
                    return
                if path == "/api/reports":
                    report_limit = entitlements.limit_for(
                        int(user["id"]),
                        "saved_reports",
                    )
                    json_response(
                        self,
                        {
                            "reports": db.list_reports(int(user["id"]), report_limit),
                            "limit": report_limit,
                        },
                    )
                    return
                if path == "/api/search":
                    query = urllib.parse.parse_qs(parsed.query)
                    q = (query.get("q") or [""])[0]
                    json_response(self, {"results": fetch_yahoo_search(q)})
                    return
                if path == "/api/ai-status":
                    json_response(self, public_ai_config())
                    return
                if path == "/api/us-momentum/schedule":
                    json_response(self, momentum_alerts.schedule_status())
                    return
                if path == "/api/robinhood-radar/market":
                    query = urllib.parse.parse_qs(parsed.query)
                    try:
                        result = robinhood_radar.market_data((query.get("asset") or [""])[0], (query.get("pool") or [""])[0])
                    except ValueError as exc:
                        json_response(self, {"error": str(exc)}, status=400)
                        return
                    json_response(self, result)
                    return
                if path == "/api/robinhood-radar":
                    query = urllib.parse.parse_qs(parsed.query)
                    force = str((query.get("refresh") or [""])[0]).lower() in {"1", "true", "yes"}
                    try:
                        result = robinhood_radar.scan(force=force)
                    except robinhood_radar.RobinhoodRadarError as exc:
                        json_response(
                            self,
                            {"error": str(exc), "code": "robinhood_radar_unavailable"},
                            status=503,
                        )
                        return
                    json_response(self, result)
                    return
                if path == "/api/trade-review":
                    entitlements.require_pro(int(user["id"]), "us_momentum")
                    if not trade_review.longbridge_configured():
                        json_response(self, trade_review.not_connected_review())
                        return
                    try:
                        executions = trade_review.load_longbridge_executions(days=7)
                        round_trips = trade_review.round_trips_from_executions(executions)
                        result = trade_review.build_trade_review(round_trips)
                        result["execution_count"] = len(executions)
                        result["provider"] = "Longbridge OpenAPI · read only"
                        result["connected"] = True
                    except Exception as exc:
                        if trade_review.looks_unconfigured(exc):
                            json_response(self, trade_review.not_connected_review())
                            return
                        json_response(
                            self,
                            {"error": f"读取 Longbridge 成交记录失败：{str(exc) or type(exc).__name__}", "code": "trade_review_failed"},
                            status=503,
                        )
                        return
                    json_response(self, result)
                    return
                if path == "/api/watchlist":
                    uid = int(user["id"])
                    watch_limit = entitlements.limit_for(uid, "watchlist_symbols")
                    symbols = db.get_watch_symbols(uid)
                    json_response(
                        self,
                        {
                            "symbols": symbols[:watch_limit],
                            "limit": watch_limit,
                            "total": len(symbols),
                        },
                    )
                    return
                if path == "/api/brief/preferences":
                    uid = int(user["id"])
                    preferences = db.get_brief_preferences(uid)
                    channel_configs, channel_summaries = user_channel_configs(uid)
                    channels = notifications.channel_status(channel_configs)
                    watch_symbols = db.get_watch_symbols(uid)
                    json_response(
                        self,
                        {
                            "preferences": preferences,
                            "channels": channels,
                            "channel_configs": channel_summaries,
                            "deliveries": db.list_notification_deliveries(uid, 20),
                            "jobs": db.list_analysis_jobs(uid, 14),
                            "setup": {
                                "watchlist_ready": bool(watch_symbols),
                                "channel_ready": any(channels.get(item, False) for item in preferences["channels"]),
                                "brief_enabled": bool(preferences["enabled"]),
                                "watch_count": len(watch_symbols),
                            },
                        },
                    )
                    return
                if path == "/api/signal-events":
                    uid = int(user["id"])
                    history_days = entitlements.limit_for(uid, "signal_history_days")
                    watch_limit = entitlements.limit_for(uid, "watchlist_symbols")
                    allowed_symbols = set(db.get_watch_symbols(uid)[:watch_limit])
                    snapshots = {
                        symbol: value
                        for symbol, value in db.get_signal_snapshots(uid).items()
                        if symbol in allowed_symbols
                    }
                    events = [
                        event
                        for event in db.list_signal_events(
                            uid,
                            history_days=history_days,
                        )
                        if str(event.get("symbol") or "").upper() in allowed_symbols
                    ]
                    json_response(
                        self,
                        {
                            "events": events,
                            "snapshots": snapshots,
                            "history_days": history_days,
                        },
                    )
                    return
                json_response(self, {"error": "Not found"}, status=404)
                return
            self.serve_static(path)
        except entitlements.EntitlementError as exc:
            json_response(self, exc.payload(), status=exc.status)
        except Exception as exc:
            internal_error(self, exc)

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/api/payments/webhook":
                raw_body = read_raw_body(self)
                try:
                    event = waffo_bridge.verify_webhook(
                        raw_body,
                        self.headers.get("X-Waffo-Signature"),
                    )
                except waffo_bridge.WaffoBridgeError as exc:
                    status = 401 if exc.status == 401 else 502
                    json_response(self, {"error": exc.message}, status=status)
                    return

                if (
                    event.get("mode") != "test"
                    or event.get("eventType") not in db.WAFFO_SUBSCRIPTION_EVENTS
                ):
                    print(
                        "[waffo] ignored verified webhook "
                        f"mode={event.get('mode')} event={event.get('eventType')}"
                    )
                    json_response(self, {"received": True, "ignored": True})
                    return

                result = db.process_waffo_subscription_event(event)
                print(
                    f"[waffo] {event.get('eventType')} "
                    f"event={event.get('eventId')} linked={result.get('linked')} "
                    f"duplicate={result.get('duplicate')}"
                )
                json_response(self, {"received": True, **result})
                return

            if path not in {"/api/auth/send-code", "/api/auth/login"} and not security.request_origin_allowed(self):
                json_response(self, {"error": "请求来源无效", "code": "invalid_origin"}, status=403)
                return

            payload = read_json_body(self)

            if path == "/api/auth/send-code":
                try:
                    result = auth.create_login_code(str(payload.get("email", "")), self.client_address[0])
                except auth.AuthError as exc:
                    json_response(self, {"error": exc.message}, status=exc.status)
                    return
                json_response(self, result)
                return

            if path == "/api/auth/login":
                try:
                    user, token = auth.verify_login_code(
                        str(payload.get("email", "")),
                        str(payload.get("code", "")),
                    )
                except auth.AuthError as exc:
                    json_response(self, {"error": exc.message}, status=exc.status)
                    return
                json_response(
                    self,
                    {
                        "user": public_user(user),
                        "entitlement": entitlements.entitlement_snapshot(int(user["id"])),
                    },
                    headers={"Set-Cookie": auth.session_cookie_header(token)},
                )
                return

            if path == "/api/auth/logout":
                auth.logout_session(self)
                json_response(
                    self,
                    {"ok": True},
                    headers={"Set-Cookie": auth.session_cookie_header("", clear=True)},
                )
                return

            user = require_user(self)
            if not user:
                return
            user_id = int(user["id"])

            if path == "/api/analyze":
                respond_analysis(self, user, normalize_symbol(str(payload.get("symbol") or "")))
                return

            if path == "/api/payments/checkout":
                external_id = str(uuid.uuid4())
                reservation = db.reserve_subscription_checkout(
                    user_id,
                    external_id,
                    str(user["email"]),
                    plan_code="pro_monthly",
                )
                if not reservation["created"]:
                    if reservation["reason"] == "checkout_in_progress":
                        existing_order = reservation["order"]
                        if resumable_checkout(existing_order):
                            json_response(
                                self,
                                {
                                    "order": existing_order["external_id"],
                                    "checkoutUrl": existing_order["checkout_url"],
                                    "expiresAt": existing_order["expires_at"],
                                    "amount": existing_order.get("amount") or "19.99",
                                    "currency": existing_order.get("currency") or "USD",
                                    "environment": "test",
                                    "resumed": True,
                                },
                            )
                            return

                        creating_is_recent = False
                        if existing_order.get("status") == "creating":
                            try:
                                updated_at = datetime.fromisoformat(
                                    str(existing_order.get("updated_at") or "")
                                    .replace("Z", "+00:00")
                                )
                                if updated_at.tzinfo is not None:
                                    updated_at = (
                                        updated_at.astimezone(timezone.utc)
                                        .replace(tzinfo=None)
                                    )
                                creating_is_recent = (
                                    datetime.now(timezone.utc).replace(tzinfo=None)
                                    - updated_at
                                ).total_seconds() < 60
                            except ValueError:
                                creating_is_recent = True
                        if creating_is_recent:
                            json_response(
                                self,
                                {
                                    "error": "Pro 订阅结账正在创建，请稍后继续。",
                                    "code": "checkout_in_progress",
                                    "order": existing_order["external_id"],
                                    "order_status": existing_order["status"],
                                },
                                status=409,
                            )
                            return

                        try:
                            provider_order = waffo_bridge.lookup_subscription_order(
                                str(existing_order["external_id"])
                            )
                        except waffo_bridge.WaffoBridgeError as exc:
                            json_response(
                                self,
                                {
                                    "error": f"无法确认上一笔订阅状态：{exc.message}",
                                    "code": "checkout_reconciliation_pending",
                                    "order": existing_order["external_id"],
                                },
                                status=503,
                            )
                            return

                        provider_status = str(
                            (provider_order or {}).get("status") or ""
                        ).lower()
                        if provider_order and provider_status == "pending":
                            try:
                                canceled = waffo_bridge.cancel_subscription(
                                    str(provider_order["id"])
                                )
                            except waffo_bridge.WaffoBridgeError as exc:
                                json_response(
                                    self,
                                    {
                                        "error": f"无法关闭上一笔未完成订阅：{exc.message}",
                                        "code": "checkout_reconciliation_pending",
                                        "order": existing_order["external_id"],
                                    },
                                    status=503,
                                )
                                return
                            canceled_status = str(
                                canceled.get("status") or ""
                            ).lower()
                            if canceled_status not in {
                                "canceled",
                                "closed",
                                "expired",
                            }:
                                json_response(
                                    self,
                                    {
                                        "error": "上一笔订阅已进入结算状态，请等待状态同步。",
                                        "code": "subscription_sync_pending",
                                        "order": existing_order["external_id"],
                                        "provider_status": canceled_status,
                                    },
                                    status=409,
                                )
                                return
                            provider_status = canceled_status
                        if provider_order and provider_status not in {
                            "closed",
                            "canceled",
                            "expired",
                        }:
                            json_response(
                                self,
                                {
                                    "error": "Waffo 已存在该账户的订阅订单，正在等待状态同步。",
                                    "code": "subscription_sync_pending",
                                    "order": existing_order["external_id"],
                                    "provider_status": provider_status,
                                },
                                status=409,
                            )
                            return

                        db.close_unfinished_payment_order(
                            user_id,
                            str(existing_order["external_id"]),
                        )
                        reservation = db.reserve_subscription_checkout(
                            user_id,
                            external_id,
                            str(user["email"]),
                            plan_code="pro_monthly",
                        )
                        if not reservation["created"]:
                            json_response(
                                self,
                                {
                                    "error": "订阅状态刚刚发生变化，请刷新后重试。",
                                    "code": "checkout_state_changed",
                                },
                                status=409,
                            )
                            return
                    else:
                        json_response(
                            self,
                            {
                                "error": "当前账户已有未结束的 Pro 订阅，请先管理现有订阅。",
                                "code": "already_subscribed",
                            },
                            status=409,
                        )
                        return
                success_url = (
                    f"{request_origin(self)}/?"
                    + urllib.parse.urlencode({"payment": external_id})
                )
                try:
                    checkout = waffo_bridge.create_checkout(
                        {
                            "userId": user_id,
                            "buyerEmail": user["email"],
                            "internalOrderId": external_id,
                            "successUrl": success_url,
                            "language": str(payload.get("locale") or "zh-CN"),
                        }
                    )
                    checkout_url = str(checkout.get("checkoutUrl") or "")
                    if urllib.parse.urlparse(checkout_url).scheme != "https":
                        raise RuntimeError("Waffo checkout URL 无效")
                    db.set_payment_checkout(user_id, external_id, checkout)
                except waffo_bridge.WaffoBridgeError as exc:
                    definitive_failure = (
                        400 <= exc.status < 500
                        and exc.status not in {408, 409, 425, 429}
                    )
                    if definitive_failure:
                        db.mark_payment_order_failed(user_id, external_id)
                    else:
                        db.mark_payment_order_unknown(user_id, external_id)
                    status = 503 if exc.status == 503 else 502
                    json_response(self, {"error": exc.message}, status=status)
                    return
                except Exception:
                    db.mark_payment_order_unknown(user_id, external_id)
                    raise

                json_response(
                    self,
                    {
                        "order": external_id,
                        "checkoutUrl": checkout_url,
                        "expiresAt": checkout.get("expiresAt"),
                        "amount": checkout.get("amount") or "19.99",
                        "currency": checkout.get("currency") or "USD",
                        "environment": "test",
                    },
                )
                return

            if path == "/api/subscription/cancel":
                subscription = db.get_subscription(user_id)
                if not subscription or subscription.get("status") in {
                    "canceled",
                    "expired",
                    "closed",
                }:
                    json_response(
                        self,
                        {
                            "error": "当前账户没有可取消的 Pro 订阅",
                            "code": "subscription_not_cancelable",
                        },
                        status=409,
                    )
                    return
                if subscription.get("status") == "canceling":
                    json_response(
                        self,
                        {
                            "subscription": subscription,
                            "pendingWebhook": False,
                        },
                    )
                    return
                try:
                    result = waffo_bridge.cancel_subscription(
                        str(subscription["provider_subscription_id"])
                    )
                except waffo_bridge.WaffoBridgeError as exc:
                    status = 503 if exc.status == 503 else 502
                    json_response(self, {"error": exc.message}, status=status)
                    return
                json_response(
                    self,
                    {
                        "result": result,
                        "subscription": subscription,
                        "pendingWebhook": True,
                    },
                )
                return

            if path == "/api/report":
                symbol = normalize_symbol(payload.get("symbol", ""))
                analysis = payload.get("analysis")
                analysis_usage = None
                if not isinstance(analysis, dict) or not analysis:
                    analysis_usage = entitlements.consume_or_raise(
                        user_id,
                        "analysis_daily",
                    )
                    try:
                        analysis = build_analysis(symbol)
                    except Exception:
                        entitlements.refund(
                            user_id,
                            "analysis_daily",
                            period_key=analysis_usage.get("period_key"),
                        )
                        raise
                ai_usage = None
                if get_ai_config(True)["configured"]:
                    ai_usage = entitlements.try_consume(
                        user_id,
                        "ai_report_monthly",
                    )
                if ai_usage is None or ai_usage["allowed"]:
                    report, ai_meta = call_ai_report(analysis)
                else:
                    report = None
                    config = get_ai_config(True)
                    ai_meta = {
                        "configured": True,
                        "used": False,
                        "provider": config["provider"],
                        "model": config["model"],
                        "quota_exceeded": True,
                    }
                if (
                    ai_usage
                    and ai_usage["allowed"]
                    and not ai_meta.get("used")
                ):
                    entitlements.refund(
                        user_id,
                        "ai_report_monthly",
                        period_key=ai_usage.get("period_key"),
                    )
                    ai_usage = None
                if report is None:
                    report = local_report(analysis)
                report_limit = entitlements.limit_for(user_id, "saved_reports")
                report_id = db.save_report(
                    user_id,
                    symbol,
                    analysis["asset_type"],
                    report,
                    max_reports=report_limit,
                )
                json_response(
                    self,
                    {
                        "id": report_id,
                        "report": report,
                        "ai": ai_meta,
                        "usage": ai_usage,
                        "analysis_usage": analysis_usage,
                        "saved_report_limit": report_limit,
                    },
                )
                return
            if path == "/api/brief/run":
                preferences = db.get_brief_preferences(user_id)
                from zoneinfo import ZoneInfo

                local_date = datetime.now(timezone.utc).astimezone(
                    ZoneInfo(preferences["timezone"])
                ).date().isoformat()
                result = run_daily_brief(
                    {
                        **preferences,
                        "user_id": user_id,
                        "email": str(user["email"]),
                        "local_date": local_date,
                    }
                )
                json_response(self, result)
                return
            if path == "/api/brief/test":
                requested = str(payload.get("channel") or "email").strip().lower()
                if requested not in BRIEF_CHANNELS:
                    json_response(self, {"error": "不支持的通知渠道"}, status=400)
                    return
                plan, _source = entitlements.plan_for_user(user_id)
                if plan != entitlements.PRO and requested != "email":
                    json_response(self, {"error": "多渠道测试推送需要 Pro", "code": "pro_required"}, status=403)
                    return
                try:
                    channel_configs, _summaries = user_channel_configs(user_id)
                    attempts = notifications.send_with_retry(
                        requested,
                        str(user["email"]),
                        "MarketBrief AI 测试推送",
                        "如果你看到这条消息，说明该通知渠道已配置成功。",
                        config=channel_configs.get(requested),
                    )
                except Exception as exc:
                    json_response(self, {"ok": False, "channel": requested, "error": str(exc)}, status=502)
                    return
                json_response(self, {"ok": True, "channel": requested, "attempts": attempts})
                return
            retry_match = re.fullmatch(r"/api/brief/deliveries/(\d+)/retry", path)
            if retry_match:
                delivery_id = int(retry_match.group(1))
                delivery = db.claim_failed_delivery(user_id, delivery_id)
                if not delivery:
                    json_response(self, {"error": "该失败记录不存在或正在重试"}, status=409)
                    return
                brief = delivery.get("brief") or {}
                items = brief.get("items") or []
                if not items:
                    db.update_notification_delivery(
                        delivery_id, user_id, status="failed",
                        attempt_count=delivery["attempt_count"], error="简报内容为空，无法重发"
                    )
                    json_response(self, {"error": "简报内容为空，无法重发"}, status=409)
                    return
                text, _payload = format_daily_brief(delivery["local_date"], items)
                try:
                    channel_configs, _summaries = user_channel_configs(user_id)
                    attempts = notifications.send_with_retry(
                        delivery["channel"], str(user["email"]),
                        f"MarketBrief AI 每日简报 · {delivery['local_date']}", text,
                        config=channel_configs.get(delivery["channel"]),
                    )
                except Exception as exc:
                    total_attempts = delivery["attempt_count"] + 3
                    db.update_notification_delivery(
                        delivery_id, user_id, status="failed",
                        attempt_count=total_attempts, error=str(exc)
                    )
                    json_response(self, {"ok": False, "error": str(exc)}, status=502)
                    return
                total_attempts = delivery["attempt_count"] + attempts
                db.update_notification_delivery(
                    delivery_id, user_id, status="sent", attempt_count=total_attempts
                )
                json_response(self, {"ok": True, "attempts": attempts})
                return
            if path == "/api/feedback":
                category = str(payload.get("category") or "general").strip().lower()
                message = str(payload.get("message") or "").strip()
                if category not in {"general", "bug", "report", "notification", "pricing"}:
                    json_response(self, {"error": "反馈分类无效"}, status=400)
                    return
                if len(message) < 5 or len(message) > 1000:
                    json_response(self, {"error": "反馈内容需为 5–1000 个字符"}, status=400)
                    return
                feedback_id = db.save_product_feedback(user_id, category, message)
                json_response(self, {"ok": True, "id": feedback_id}, status=201)
                return
            if path == "/api/admin/site-footer":
                if not is_site_admin(user):
                    json_response(self, {"error": "仅站点管理员可编辑页脚"}, status=403)
                    return
                try:
                    footer = normalize_site_footer(payload)
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                db.save_site_setting("footer", footer, user_id)
                json_response(self, {"ok": True, "footer": footer})
                return
            if path == "/api/scan":
                symbols = payload.get("symbols") or []
                if not isinstance(symbols, list):
                    json_response(self, {"error": "symbols must be a list"}, status=400)
                    return
                try:
                    requested_symbols = list(
                        dict.fromkeys(
                            normalize_symbol(str(item))
                            for item in symbols
                            if str(item).strip()
                        )
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                if not requested_symbols:
                    json_response(
                        self,
                        {"error": "请至少提供一个扫描标的"},
                        status=400,
                    )
                    return
                scan_limit = entitlements.ensure_count_within_limit(
                    user_id,
                    "scan_symbols",
                    len(requested_symbols),
                )
                usage = entitlements.consume_or_raise(user_id, "scan_daily")
                try:
                    results = scan_symbols(
                        requested_symbols[:scan_limit]
                    )
                except Exception:
                    entitlements.refund(
                        user_id,
                        "scan_daily",
                        period_key=usage.get("period_key"),
                    )
                    raise
                if not any(not item.get("error") for item in results):
                    entitlements.refund(
                        user_id,
                        "scan_daily",
                        period_key=usage.get("period_key"),
                    )
                    json_response(
                        self,
                        {
                            "error": "所有标的扫描均失败，本次未计入额度",
                            "results": results,
                        },
                        status=502,
                    )
                    return
                json_response(
                    self,
                    {
                        "results": results,
                        "usage": usage,
                    },
                )
                return
            if path == "/api/us-momentum/scan":
                entitlements.require_pro(user_id, "us_momentum")
                try:
                    result = momentum_scanner.scan_us_momentum(limit=20)
                except momentum_scanner.MomentumScannerError as exc:
                    json_response(self, {"error": str(exc), "code": "momentum_scan_failed"}, status=503)
                    return
                json_response(self, result)
                return
            if path == "/api/signal-events":
                events = payload.get("events") or []
                if not isinstance(events, list):
                    json_response(self, {"error": "events must be a list"}, status=400)
                    return
                snapshots = payload.get("snapshots") or {}
                if not isinstance(snapshots, dict):
                    json_response(self, {"error": "snapshots must be an object"}, status=400)
                    return
                event_limit = entitlements.limit_for(user_id, "scan_symbols")
                snapshot_limit = entitlements.limit_for(user_id, "watchlist_symbols")
                if len(events) > event_limit or len(snapshots) > snapshot_limit:
                    json_response(
                        self,
                        {
                            "error": "信号记录数量超过当前套餐限制",
                            "code": "quota_exceeded",
                        },
                        status=429,
                    )
                    return
                allowed_symbols = set(
                    db.get_watch_symbols(user_id)[:snapshot_limit]
                )
                submitted_symbols = {
                    str(item.get("symbol") or "").strip().upper()
                    for item in events
                    if isinstance(item, dict)
                } | {
                    str(symbol).strip().upper()
                    for symbol in snapshots
                }
                if not submitted_symbols.issubset(allowed_symbols):
                    json_response(
                        self,
                        {"error": "只能保存当前关注列表中的信号记录"},
                        status=400,
                    )
                    return
                created_ids: list[int] = []
                for item in events:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol", "")).strip().upper()
                    if not symbol:
                        continue
                    created_ids.append(db.add_signal_event(user_id, symbol, item))
                for symbol, snap in snapshots.items():
                    if isinstance(snap, dict):
                        db.upsert_signal_snapshot(user_id, str(symbol).strip().upper(), snap)
                history_days = entitlements.limit_for(
                    user_id,
                    "signal_history_days",
                )
                db.prune_signal_events(user_id, history_days)
                json_response(
                    self,
                    {
                        "created": created_ids,
                        "events": db.list_signal_events(
                            user_id,
                            history_days=history_days,
                        ),
                    },
                )
                return
            json_response(self, {"error": "Not found"}, status=404)
        except entitlements.EntitlementError as exc:
            json_response(self, exc.payload(), status=exc.status)
        except Exception as exc:
            internal_error(self, exc)

    def do_PUT(self) -> None:
        try:
            if not security.request_origin_allowed(self):
                json_response(self, {"error": "请求来源无效", "code": "invalid_origin"}, status=403)
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            user = require_user(self)
            if not user:
                return
            payload = read_json_body(self)
            if path == "/api/watchlist":
                symbols = payload.get("symbols") or []
                if not isinstance(symbols, list):
                    json_response(self, {"error": "symbols must be a list"}, status=400)
                    return
                try:
                    normalized_symbols = [
                        normalize_symbol(str(item))
                        for item in symbols
                        if str(item).strip()
                    ]
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                unique_symbols = set(normalized_symbols)
                max_symbols = entitlements.ensure_count_within_limit(
                    int(user["id"]),
                    "watchlist_symbols",
                    len(unique_symbols),
                )
                saved = db.set_watch_symbols(
                    int(user["id"]),
                    normalized_symbols,
                    max_symbols=max_symbols,
                )
                json_response(self, {"symbols": saved})
                return
            if path == "/api/brief/preferences":
                try:
                    preferences = normalize_brief_preferences(payload)
                except (TypeError, ValueError) as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                plan, _source = entitlements.plan_for_user(int(user["id"]))
                if plan != entitlements.PRO and any(
                    channel != "email" for channel in preferences["channels"]
                ):
                    json_response(
                        self,
                        {
                            "error": "Free 套餐仅支持邮件简报；多渠道推送需要 Pro。",
                            "code": "pro_required",
                        },
                        status=403,
                    )
                    return
                saved = db.save_brief_preferences(int(user["id"]), preferences)
                channel_configs, _summaries = user_channel_configs(int(user["id"]))
                json_response(self, {"preferences": saved, "channels": notifications.channel_status(channel_configs)})
                return
            if path == "/api/brief/channel-config":
                channel = str(payload.get("channel") or "").strip().lower()
                plan, _source = entitlements.plan_for_user(int(user["id"]))
                if plan != entitlements.PRO:
                    json_response(self, {"error": "用户级多渠道推送需要 Pro", "code": "pro_required"}, status=403)
                    return
                try:
                    config, masked_label = normalize_channel_config(channel, payload)
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, status=400)
                    return
                encrypted = secret_store.encrypt_config(config)
                db.save_notification_channel_config(int(user["id"]), channel, encrypted, masked_label)
                json_response(self, {"ok": True, "channel": channel, "label": masked_label})
                return
            json_response(self, {"error": "Not found"}, status=404)
        except entitlements.EntitlementError as exc:
            json_response(self, exc.payload(), status=exc.status)
        except Exception as exc:
            internal_error(self, exc)

    def do_DELETE(self) -> None:
        try:
            if not security.request_origin_allowed(self):
                json_response(self, {"error": "请求来源无效", "code": "invalid_origin"}, status=403)
                return
            parsed = urllib.parse.urlparse(self.path)
            channel_match = re.fullmatch(r"/api/brief/channel-config/(wechat|feishu|telegram)", parsed.path)
            if channel_match:
                user = require_user(self)
                if not user:
                    return
                deleted = db.delete_notification_channel_config(int(user["id"]), channel_match.group(1))
                json_response(self, {"deleted": deleted})
                return
            match = re.fullmatch(r"/api/reports/(\d+)", parsed.path)
            if not match:
                json_response(self, {"error": "Not found"}, status=404)
                return
            user = require_user(self)
            if not user:
                return
            report_id = int(match.group(1))
            if not db.delete_report(int(user["id"]), report_id):
                json_response(self, {"error": "报告不存在"}, status=404)
                return
            json_response(self, {"deleted": True, "id": report_id})
        except Exception as exc:
            internal_error(self, exc)

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        else:
            safe = path.lstrip("/")
            file_path = (STATIC_DIR / safe).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                text_response(self, b"Forbidden", "text/plain", 403)
                return
            if file_path.is_dir():
                file_path = file_path / "index.html"
        if not file_path.exists() or not file_path.is_file():
            text_response(self, b"Not found", "text/plain", 404)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(file_path.suffix, "application/octet-stream")
        text_response(self, file_path.read_bytes(), content_type)


def main() -> None:
    load_env_file()
    db.ensure_schema()
    waffo_bridge.start_sidecar()
    scheduler_mode = os.environ.get("BRIEF_SCHEDULER_MODE", "embedded").strip().lower()
    scheduler = brief_scheduler.BriefScheduler(run_daily_brief)
    momentum_scheduler = momentum_alerts.MomentumAlertScheduler()
    if scheduler_mode == "embedded":
        scheduler.start()
        momentum_scheduler.start()
    preferred_port = DEFAULT_PORT
    if len(sys.argv) > 1:
        preferred_port = int(sys.argv[1])
    server: ThreadingHTTPServer | None = None
    try:
        server, port = create_server(preferred_port)
        print(f"MarketBrief AI running at http://127.0.0.1:{port}")
        print("Press Ctrl+C to stop.")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if scheduler_mode == "embedded":
            momentum_scheduler.stop()
            scheduler.stop()
        if server:
            server.server_close()
        waffo_bridge.stop_sidecar()


def create_server(preferred_port: int) -> tuple[ThreadingHTTPServer, int]:
    for port in range(preferred_port, preferred_port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler), port
        except OSError as exc:
            if exc.errno not in {48, 98, 10048}:
                raise
            print(f"Port {port} is already in use, trying {port + 1}...")
    raise OSError(f"No available local port from {preferred_port} to {preferred_port + 19}")


if __name__ == "__main__":
    main()
