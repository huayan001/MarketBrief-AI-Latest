"""Scheduled Telegram alerts for newly triggered US momentum candidates."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, time as wall_time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import db
import entitlements
import momentum_scanner
import momentum_strategy
import notifications
import secret_store
import trade_review


MARKET_TIMEZONE = ZoneInfo("America/New_York")
WINDOW_START = wall_time(9, 25)
WINDOW_END = wall_time(9, 50)


def _enabled() -> bool:
    return os.environ.get("MOMENTUM_TELEGRAM_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def interval_seconds() -> int:
    try:
        return max(60, int(os.environ.get("MOMENTUM_SCAN_INTERVAL_SECONDS", "600")))
    except ValueError:
        return 600


def cooldown_seconds() -> int:
    try:
        return max(300, int(os.environ.get("MOMENTUM_ALERT_COOLDOWN_SECONDS", "14400")))
    except ValueError:
        return 14400


def _scan_slots(local_date) -> list[datetime]:
    start = datetime.combine(local_date, WINDOW_START, tzinfo=MARKET_TIMEZONE)
    end = datetime.combine(local_date, WINDOW_END, tzinfo=MARKET_TIMEZONE)
    step = timedelta(seconds=interval_seconds())
    slots = []
    current = start
    while current <= end:
        slots.append(current)
        current += step
    return slots


def next_scan_at(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    local_date = current.astimezone(MARKET_TIMEZONE).date()
    for day_offset in range(8):
        candidate_date = local_date + timedelta(days=day_offset)
        if candidate_date.weekday() >= 5:
            continue
        for local_slot in _scan_slots(candidate_date):
            slot = local_slot.astimezone(timezone.utc)
            if slot >= current:
                return slot
    raise RuntimeError("无法计算下一次美股开盘扫描时间")


def schedule_status(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(MARKET_TIMEZONE)
    in_window = local.weekday() < 5 and WINDOW_START <= local.time().replace(tzinfo=None) <= WINDOW_END
    next_scan = next_scan_at(current)
    return {
        "timezone": "America/New_York",
        "window": "09:25–09:50 ET",
        "scan_times": ["09:25", "09:35", "09:45"],
        "in_recommended_window": in_window,
        "market_time": local.isoformat(),
        "next_scan_at": next_scan.isoformat(),
        "next_scan_market_time": next_scan.astimezone(MARKET_TIMEZONE).isoformat(),
    }


class MomentumAlertRunner:
    def __init__(self, *, scan: Callable[..., dict[str, Any]] = momentum_scanner.scan_us_momentum,
                 send: Callable[..., int] = notifications.send_with_retry,
                 claim: Callable[[int, str, int], bool] = db.claim_momentum_alert,
                 release: Callable[[int, str], None] = db.release_momentum_alert_claim,
                 risk_state: Callable[[], dict[str, Any]] | None = None) -> None:
        self.scan, self.send = scan, send
        self.claim, self.release = claim, release
        self.risk_state = risk_state or (lambda: {"consecutive_losses": 0, "daily_loss_pct": 0})

    def run_once(self) -> dict[str, Any]:
        if not _enabled():
            return {"scanned": False, "sent": 0, "reason": "disabled"}
        recipients = self._telegram_recipients()
        if not recipients:
            return {"scanned": False, "sent": 0, "reason": "no_recipients"}
        try:
            gate = momentum_strategy.risk_gate(**self.risk_state())
        except Exception as exc:
            print(f"[momentum-alerts] risk state unavailable: {type(exc).__name__}")
            gate = {"allow_new_alerts": True, "reasons": ["risk_state_unavailable"]}
        if not gate["allow_new_alerts"]:
            return {"scanned": False, "sent": 0, "reason": "risk_gate", "risk_reasons": gate["reasons"]}
        result = self.scan(limit=20)
        alerts = [item for item in result.get("candidates", []) if item.get("alert")]
        if not alerts:
            return {"scanned": True, "sent": 0, "reason": "no_alerts"}
        cooldown, sent = cooldown_seconds(), 0
        for recipient in recipients:
            user_id = recipient["user_id"]
            pending = [item for item in alerts if self.claim(user_id, item["symbol"], cooldown)]
            if not pending:
                continue
            try:
                self.send("telegram", "", "美股强势雷达提醒", self._format_message(pending, result),
                          config=recipient["config"])
            except Exception as exc:
                print(f"[momentum-alerts] Telegram user {user_id} failed: {exc}")
                for item in pending:
                    self.release(user_id, item["symbol"])
                continue
            sent += 1
        return {"scanned": True, "sent": sent, "alert_count": len(alerts)}

    @staticmethod
    def _telegram_recipients() -> list[dict[str, Any]]:
        recipients = []
        for row in db.list_notification_channel_configs_by_channel("telegram"):
            user_id = int(row["user_id"])
            plan, _source = entitlements.plan_for_user(user_id)
            if plan != entitlements.PRO:
                continue
            try:
                config = secret_store.decrypt_config(row["encrypted"])
            except RuntimeError:
                continue
            if config.get("bot_token") and config.get("chat_id"):
                recipients.append({"user_id": user_id, "config": config})
        return recipients

    @staticmethod
    def _format_message(items: list[dict[str, Any]], result: dict[str, Any]) -> str:
        lines = [f"数据源：{result.get('provider', 'Longbridge OpenAPI')}（仅提醒，不下单）"]
        for item in items:
            float_text = "未知" if item.get("float_shares") is None else f"{item['float_shares'] / 1_000_000:.2f}M"
            ratio_text = "未知" if item.get("volume_float_ratio") is None else f"{item['volume_float_ratio']:.2f}x"
            lines.extend(["", f"{item['symbol']}｜评分 {item['score']}",
                          f"价格 ${item['price']:.2f}｜涨幅 {item['change_pct']:.2f}%",
                          f"流通盘 {float_text}｜量/流通盘 {ratio_text}", "MACD：零轴上方"])
        lines.extend(["", "风险提示：小盘强势股波动和滑点极高，请人工确认。"])
        return "\n".join(lines)


class MomentumAlertScheduler:
    def __init__(self, runner: MomentumAlertRunner | None = None) -> None:
        self.runner = runner or MomentumAlertRunner(risk_state=trade_review.current_risk_state)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="momentum-alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            next_run = next_scan_at()
            delay = max(0.0, (next_run - datetime.now(timezone.utc)).total_seconds())
            if self._stop.wait(delay):
                break
            try:
                result = self.runner.run_once()
                if result.get("sent"):
                    print(f"[momentum-alerts] sent to {result['sent']} Telegram recipient(s)")
            except Exception as exc:
                print(f"[momentum-alerts] scan failed: {exc}")
            if self._stop.wait(1):
                break
