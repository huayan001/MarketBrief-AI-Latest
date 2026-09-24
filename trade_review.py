"""Read-only trade-review calculations and Longbridge execution import."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def merge_order_metadata(
    executions: list[dict[str, Any]], orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(order.get("order_id") or ""): order for order in orders}
    return [{**row, **{
        "side": row.get("side") or by_id.get(str(row.get("order_id") or ""), {}).get("side", ""),
        "order_type": row.get("order_type") or by_id.get(str(row.get("order_id") or ""), {}).get("order_type", ""),
    }} for row in executions]


def _retry_read(operation, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.6 * (attempt + 1))
    raise last_error


def _issues(trade: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    entry_time = str(trade.get("entry_time") or "")[-5:]
    if "09:30" <= entry_time < "09:35":
        issues.append("开盘确认不足")
    if str(trade.get("entry_type") or "").upper() == "MARKET" and (
        _number(trade.get("spread_pct")) or 0
    ) > 1:
        issues.append("市价单滑点风险")
    market_cap = _number(trade.get("market_cap"))
    if market_cap is not None and market_cap < 100_000_000:
        issues.append("选股质量不足")
    if (_number(trade.get("pnl")) or 0) < 0 and not issues:
        issues.append("退出或止损需要复核")
    return issues


def not_connected_review() -> dict[str, Any]:
    """Empty review used when Longbridge is not configured or not authorized."""
    payload = build_trade_review([])
    payload.update({
        "connected": False,
        "execution_count": 0,
        "provider": "Longbridge OpenAPI · read only",
        "code": "not_connected",
    })
    return payload


def longbridge_configured() -> bool:
    try:
        import momentum_scanner

        momentum_scanner._client_id()
    except Exception:
        return False
    return True


def looks_unconfigured(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        needle in text
        for needle in (
            "oauth",
            "授权",
            "未找到 longbridge",
            "尚未安装",
            "longbridge_oauth",
        )
    )


def build_trade_review(trades: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [{**trade, "issues": _issues(trade)} for trade in trades]
    pnls = [value for trade in trades if (value := _number(trade.get("pnl"))) is not None]
    wins, losses = [v for v in pnls if v > 0], [v for v in pnls if v < 0]
    issue_counts: dict[str, int] = {}
    for trade in reviewed:
        for issue in trade["issues"]:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    return {
        "summary": {
            "trade_count": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "average_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "average_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        },
        "issue_counts": issue_counts,
        "trades": reviewed,
    }


def _market_clock(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MARKET_TIMEZONE).strftime("%H:%M")


def _market_date(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MARKET_TIMEZONE).date().isoformat()


def round_trips_from_executions(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions: dict[str, list[dict[str, Any]]] = {}
    completed: list[dict[str, Any]] = []
    for execution in sorted(executions, key=lambda item: str(item.get("trade_done_at") or "")):
        symbol = str(execution.get("symbol") or "")
        side = str(execution.get("side") or "").upper()
        quantity = _number(execution.get("quantity")) or 0
        price = _number(execution.get("price")) or 0
        if not symbol or quantity <= 0 or price <= 0:
            continue
        if "BUY" in side:
            positions.setdefault(symbol, []).append({**execution, "remaining": quantity})
            continue
        if "SELL" not in side:
            continue
        remaining = quantity
        for lot in positions.setdefault(symbol, []):
            if remaining <= 0:
                break
            matched = min(remaining, lot["remaining"])
            completed.append({
                "symbol": symbol,
                "quantity": matched,
                "entry_price": _number(lot.get("price")),
                "exit_price": price,
                "entry_time": _market_clock(lot.get("trade_done_at")),
                "exit_time": _market_clock(execution.get("trade_done_at")),
                "exit_date": _market_date(execution.get("trade_done_at")),
                "entry_type": lot.get("order_type") or "UNKNOWN",
                "pnl": round((price - float(lot["price"])) * matched, 2),
            })
            lot["remaining"] -= matched
            remaining -= matched
        positions[symbol] = [lot for lot in positions[symbol] if lot["remaining"] > 0]
    return completed


def load_longbridge_executions(days: int = 7) -> list[dict[str, Any]]:
    """Fetch executions only; this function cannot place, replace, or cancel orders."""
    from longbridge.openapi import Config, OAuthBuilder, TradeContext
    from momentum_scanner import _client_id

    def no_interactive_auth(url: str) -> None:
        raise RuntimeError(f"Longbridge 授权已失效，请重新授权：{url}")

    ctx = TradeContext(Config.from_oauth(OAuthBuilder(_client_id()).build(no_interactive_auth)))
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=max(1, min(days, 30)))
    executions = _retry_read(lambda: ctx.history_executions(start_at=start_at, end_at=end_at))
    orders = _retry_read(lambda: ctx.history_orders(start_at=start_at, end_at=end_at))
    execution_rows = [{
        "symbol": str(getattr(item, "symbol", "")).removesuffix(".US"),
        "side": _enum_text(getattr(item, "side", "")),
        "quantity": _number(getattr(item, "quantity", None)),
        "price": _number(getattr(item, "price", None)),
        "trade_done_at": str(getattr(item, "trade_done_at", "")),
        "order_id": str(getattr(item, "order_id", "")),
    } for item in executions]
    order_rows = [{
        "order_id": str(getattr(item, "order_id", "")),
        "side": _enum_text(getattr(item, "side", "")),
        "order_type": _enum_text(getattr(item, "order_type", "")),
    } for item in orders]
    return merge_order_metadata(execution_rows, order_rows)


def current_risk_state() -> dict[str, Any]:
    completed = round_trips_from_executions(load_longbridge_executions(days=2))
    consecutive_losses = 0
    for trade in reversed(completed):
        if (_number(trade.get("pnl")) or 0) >= 0:
            break
        consecutive_losses += 1
    today = datetime.now(MARKET_TIMEZONE).date().isoformat()
    today_pnl = sum((_number(trade.get("pnl")) or 0) for trade in completed if trade.get("exit_date") == today)
    account_size = max(1, float(__import__("os").environ.get("MOMENTUM_ACCOUNT_EQUITY", "2000000")))
    return {
        "consecutive_losses": consecutive_losses,
        "daily_loss_pct": max(0, -today_pnl / account_size * 100),
    }
