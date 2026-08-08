"""Free/Pro plan limits and server-side usage enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import db


FREE = "free"
PRO = "pro"
PRO_PLAN_CODE = "pro_monthly"

PLAN_LIMITS: dict[str, dict[str, int]] = {
    FREE: {
        "analysis_daily": 10,
        "watchlist_symbols": 5,
        "scan_daily": 1,
        "scan_symbols": 5,
        "ai_report_monthly": 5,
        "saved_reports": 10,
        "signal_history_days": 7,
    },
    PRO: {
        "analysis_daily": 200,
        "watchlist_symbols": 30,
        "scan_daily": 10,
        "scan_symbols": 30,
        "ai_report_monthly": 100,
        "saved_reports": 200,
        "signal_history_days": 90,
    },
}

USAGE_PERIODS = {
    "analysis_daily": "day",
    "scan_daily": "day",
    "ai_report_monthly": "month",
}


class EntitlementError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int,
        metric: str | None = None,
        limit: int | None = None,
        used: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.metric = metric
        self.limit = limit
        self.used = used

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "error": self.message,
            "code": self.code,
        }
        if self.metric:
            value["metric"] = self.metric
        if self.limit is not None:
            value["limit"] = self.limit
        if self.used is not None:
            value["used"] = self.used
            value["remaining"] = max(0, (self.limit or 0) - self.used)
        return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _period_key(metric: str, now: datetime | None = None) -> str:
    current = now or _utc_now()
    if USAGE_PERIODS.get(metric) == "month":
        return current.strftime("%Y-%m")
    return current.strftime("%Y-%m-%d")


def _reset_at(metric: str, now: datetime | None = None) -> str:
    current = (now or _utc_now()).astimezone(timezone.utc)
    if USAGE_PERIODS.get(metric) == "month":
        if current.month == 12:
            reset = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            reset = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(
            current.year,
            current.month,
            current.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1)
    return reset.isoformat().replace("+00:00", "Z")


def plan_for_user(user_id: int) -> tuple[str, dict[str, Any] | None]:
    source = db.get_active_entitlement(user_id)
    return (
        PRO if source and source.get("plan_code") == PRO_PLAN_CODE else FREE,
        source,
    )


def limit_for(user_id: int, metric: str) -> int:
    plan, _source = plan_for_user(user_id)
    try:
        return PLAN_LIMITS[plan][metric]
    except KeyError as exc:
        raise ValueError(f"未知权益指标：{metric}") from exc


def entitlement_snapshot(
    user_id: int,
    *,
    include_subscription: bool = True,
) -> dict[str, Any]:
    now = _utc_now()
    plan, source = plan_for_user(user_id)
    limits = dict(PLAN_LIMITS[plan])
    period_keys = {
        metric: _period_key(metric, now)
        for metric in USAGE_PERIODS
    }
    counts = db.get_usage_counts(user_id, period_keys)
    usage = {
        metric: {
            "used": counts.get(metric, 0),
            "limit": limits[metric],
            "remaining": max(0, limits[metric] - counts.get(metric, 0)),
            "period": USAGE_PERIODS[metric],
            "reset_at": _reset_at(metric, now),
        }
        for metric in USAGE_PERIODS
    }
    active_until = source.get("active_until") if source else None
    result: dict[str, Any] = {
        "plan": plan,
        "plan_code": source.get("plan_code") if source else FREE,
        "status": source.get("status") if source else "free",
        "source": source.get("source") if source else "free",
        "active_until": (
            active_until.isoformat()
            if active_until is not None and hasattr(active_until, "isoformat")
            else active_until
        ),
        "limits": limits,
        "usage": usage,
    }
    if include_subscription:
        result["subscription"] = db.get_subscription(user_id)
    return result


def try_consume(user_id: int, metric: str, amount: int = 1) -> dict[str, Any]:
    plan, _source = plan_for_user(user_id)
    if metric not in USAGE_PERIODS:
        raise ValueError(f"指标不支持用量计数：{metric}")
    limit = PLAN_LIMITS[plan][metric]
    now = _utc_now()
    period_key = _period_key(metric, now)
    allowed, used = db.consume_usage(user_id, metric, period_key, limit, amount)
    return {
        "allowed": allowed,
        "plan": plan,
        "metric": metric,
        "period_key": period_key,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "period": USAGE_PERIODS[metric],
        "reset_at": _reset_at(metric, now),
    }


def consume_or_raise(user_id: int, metric: str, amount: int = 1) -> dict[str, Any]:
    usage = try_consume(user_id, metric, amount)
    if usage["allowed"]:
        return usage
    raise EntitlementError(
        "当前套餐额度已用完，请在额度重置后继续或升级 Pro。",
        code="quota_exceeded",
        status=429,
        metric=metric,
        limit=int(usage["limit"]),
        used=int(usage["used"]),
    )


def refund(
    user_id: int,
    metric: str,
    amount: int = 1,
    *,
    period_key: str | None = None,
) -> None:
    if metric not in USAGE_PERIODS:
        raise ValueError(f"指标不支持用量计数：{metric}")
    db.refund_usage(
        user_id,
        metric,
        period_key or _period_key(metric),
        amount,
    )


def ensure_count_within_limit(user_id: int, metric: str, count: int) -> int:
    limit = limit_for(user_id, metric)
    if count <= limit:
        return limit
    raise EntitlementError(
        f"当前套餐最多支持 {limit} 个标的。",
        code="quota_exceeded",
        status=429,
        metric=metric,
        limit=limit,
        used=count,
    )


def require_pro(user_id: int, feature: str) -> None:
    plan, _source = plan_for_user(user_id)
    if plan == PRO:
        return
    raise EntitlementError(
        "该功能需要 Pro 套餐。",
        code="pro_required",
        status=403,
        metric=feature,
    )
