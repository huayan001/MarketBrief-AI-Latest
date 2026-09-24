"""Public decision rules for the read-only US momentum radar."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")


def evaluate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    price = float(candidate.get("price") or 0)
    volume = float(candidate.get("volume") or 0)
    market_cap = float(candidate.get("market_cap") or 0)
    bid = float(candidate.get("bid") or 0)
    ask = float(candidate.get("ask") or 0)
    midpoint = (bid + ask) / 2 if bid > 0 and ask >= bid else 0
    spread_pct = (ask - bid) / midpoint * 100 if midpoint else None
    dollar_volume = price * volume
    checks = {
        "price": price >= 3,
        "dollar_volume": dollar_volume >= 10_000_000,
        "market_cap": market_cap >= 100_000_000,
        "spread": spread_pct is not None and spread_pct <= 1,
    }
    return {
        "eligible": all(checks.values()),
        "quality_checks": checks,
        "rejection_reasons": [name for name, passed in checks.items() if not passed],
        "dollar_volume": dollar_volume,
        "spread_pct": spread_pct,
    }


def opening_decision(
    now: datetime,
    *,
    breakout: bool = False,
    volume_confirmed: bool = False,
    bar_closed: bool = False,
) -> dict[str, Any]:
    local = now.astimezone(MARKET_TIMEZONE)
    clock = local.time().replace(tzinfo=None)
    if clock < time(9, 25) or clock > time(9, 50):
        return {"phase": "closed", "notify": False, "reason": "outside_window"}
    if clock < time(9, 30):
        return {"phase": "candidate_pool", "notify": False, "reason": "preopen_pool_only"}
    if clock < time(9, 35):
        return {"phase": "observe", "notify": False, "reason": "opening_noise"}
    confirmed = breakout and volume_confirmed and bar_closed
    return {
        "phase": "confirm",
        "notify": confirmed,
        "reason": "confirmed" if confirmed else "confirmation_missing",
    }


def risk_gate(
    *, consecutive_losses: int = 0, daily_loss_pct: float = 0,
    open_positions: int = 0, max_positions: int = 3,
) -> dict[str, Any]:
    reasons = []
    if consecutive_losses >= 2:
        reasons.append("two_consecutive_losses")
    if daily_loss_pct >= 0.30:
        reasons.append("daily_loss_limit")
    if open_positions >= max_positions:
        reasons.append("position_limit")
    return {"allow_new_alerts": not reasons, "reasons": reasons}
