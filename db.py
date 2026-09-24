"""MySQL access layer for MarketBrief_AI."""

from __future__ import annotations

import calendar
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mysql_config() -> dict[str, Any]:
    host = os.environ.get("MYSQL_HOST", "127.0.0.1").strip()
    user = os.environ.get("MYSQL_USER", "").strip()
    password = os.environ.get("MYSQL_PASSWORD", "")
    database = os.environ.get("MYSQL_DATABASE", "").strip()
    port_raw = os.environ.get("MYSQL_PORT", "3306").strip() or "3306"
    if not user or not database:
        raise RuntimeError("请在 api_keys.env 中配置 MYSQL_USER 与 MYSQL_DATABASE")
    return {
        "host": host or "127.0.0.1",
        "port": int(port_raw),
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
    }


@contextmanager
def get_conn() -> Iterator[pymysql.Connection]:
    conn = pymysql.connect(**mysql_config())
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            last_login_at DATETIME NULL,
            UNIQUE KEY uk_users_email (email)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS email_login_codes (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            code_hash CHAR(64) NOT NULL,
            expires_at DATETIME NOT NULL,
            consumed_at DATETIME NULL,
            failed_attempts INT UNSIGNED NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            request_ip VARCHAR(64) NULL,
            KEY idx_email_login_codes_email_created (email, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            token_hash CHAR(64) NOT NULL,
            expires_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            UNIQUE KEY uk_sessions_token_hash (token_hash),
            KEY idx_sessions_user_id (user_id),
            CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS reports (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            asset_type VARCHAR(64) NOT NULL,
            stance VARCHAR(32) NOT NULL,
            summary TEXT NOT NULL,
            report_json LONGTEXT NOT NULL,
            created_at DATETIME NOT NULL,
            KEY idx_reports_user_created (user_id, created_at),
            CONSTRAINT fk_reports_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS watch_symbols (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            UNIQUE KEY uk_watch_user_symbol (user_id, symbol),
            CONSTRAINT fk_watch_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS signal_snapshots (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            payload_json JSON NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uk_signal_snap_user_symbol (user_id, symbol),
            CONSTRAINT fk_signal_snap_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS signal_events (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            payload_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            KEY idx_signal_events_user_created (user_id, created_at),
            CONSTRAINT fk_signal_events_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS payment_orders (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            external_id VARCHAR(64) NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            provider VARCHAR(32) NOT NULL DEFAULT 'waffo',
            environment VARCHAR(16) NOT NULL DEFAULT 'test',
            order_kind VARCHAR(32) NOT NULL DEFAULT 'subscription',
            plan_code VARCHAR(32) NULL,
            status VARCHAR(32) NOT NULL,
            store_id VARCHAR(128) NULL,
            product_id VARCHAR(128) NULL,
            checkout_session_id VARCHAR(191) NULL,
            checkout_url VARCHAR(2048) NULL,
            provider_order_id VARCHAR(128) NULL,
            payment_id VARCHAR(128) NULL,
            buyer_email VARCHAR(255) NOT NULL,
            currency CHAR(3) NULL,
            amount DECIMAL(18, 2) NULL,
            expires_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            completed_at DATETIME NULL,
            UNIQUE KEY uk_payment_orders_external (external_id),
            UNIQUE KEY uk_payment_orders_session (checkout_session_id),
            UNIQUE KEY uk_payment_orders_provider_order (provider_order_id),
            UNIQUE KEY uk_payment_orders_payment (payment_id),
            KEY idx_payment_orders_user_created (user_id, created_at),
            CONSTRAINT fk_payment_orders_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            provider VARCHAR(32) NOT NULL DEFAULT 'waffo',
            environment VARCHAR(16) NOT NULL DEFAULT 'test',
            provider_subscription_id VARCHAR(128) NOT NULL,
            checkout_external_id VARCHAR(64) NULL,
            store_id VARCHAR(128) NOT NULL,
            product_id VARCHAR(128) NOT NULL,
            plan_code VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            buyer_identity VARCHAR(191) NOT NULL,
            buyer_email VARCHAR(255) NOT NULL,
            currency CHAR(3) NOT NULL,
            amount DECIMAL(18, 2) NOT NULL,
            billing_period VARCHAR(32) NOT NULL,
            current_period_start DATETIME NULL,
            current_period_end DATETIME NOT NULL,
            grace_until DATETIME NULL,
            canceled_at DATETIME NULL,
            last_event_at DATETIME(6) NOT NULL,
            last_event_type VARCHAR(64) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY uk_subscriptions_provider_order (
                provider, environment, provider_subscription_id
            ),
            KEY idx_subscriptions_user_status (user_id, status, current_period_end),
            CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS usage_counters (
            user_id BIGINT UNSIGNED NOT NULL,
            metric VARCHAR(64) NOT NULL,
            period_key VARCHAR(16) NOT NULL,
            used_count INT UNSIGNED NOT NULL DEFAULT 0,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (user_id, metric, period_key),
            CONSTRAINT fk_usage_counters_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS entitlement_grants (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            grant_code VARCHAR(64) NOT NULL,
            plan_code VARCHAR(32) NOT NULL,
            starts_at DATETIME NOT NULL,
            ends_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL,
            UNIQUE KEY uk_entitlement_grants_user_code (user_id, grant_code),
            KEY idx_entitlement_grants_user_end (user_id, ends_at),
            CONSTRAINT fk_entitlement_grants_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key VARCHAR(128) NOT NULL PRIMARY KEY,
            applied_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS payment_webhook_events (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            delivery_id VARCHAR(191) NOT NULL,
            event_type VARCHAR(64) NOT NULL,
            event_id VARCHAR(191) NOT NULL,
            event_timestamp DATETIME(6) NULL,
            mode VARCHAR(16) NOT NULL,
            external_order_id VARCHAR(64) NULL,
            payload_json JSON NOT NULL,
            received_at DATETIME NOT NULL,
            processed_at DATETIME NULL,
            KEY idx_payment_webhook_delivery (delivery_id),
            UNIQUE KEY uk_payment_webhook_occurrence (
                event_type, event_id, event_timestamp
            ),
            KEY idx_payment_webhook_business_event (event_type, event_id),
            KEY idx_payment_webhook_received (received_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS brief_preferences (
            user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            enabled TINYINT(1) NOT NULL DEFAULT 0,
            delivery_time CHAR(5) NOT NULL DEFAULT '18:00',
            timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            channels_json JSON NOT NULL,
            only_changes TINYINT(1) NOT NULL DEFAULT 1,
            min_confidence DECIMAL(5, 2) NOT NULL DEFAULT 40.00,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT fk_brief_preferences_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS analysis_jobs (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            local_date DATE NOT NULL,
            status VARCHAR(24) NOT NULL,
            symbols_json JSON NOT NULL,
            brief_json LONGTEXT NULL,
            error_message VARCHAR(500) NULL,
            started_at DATETIME NOT NULL,
            completed_at DATETIME NULL,
            UNIQUE KEY uk_analysis_jobs_user_date (user_id, local_date),
            KEY idx_analysis_jobs_status_started (status, started_at),
            CONSTRAINT fk_analysis_jobs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            job_id BIGINT UNSIGNED NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            channel VARCHAR(32) NOT NULL,
            status VARCHAR(24) NOT NULL,
            attempt_count INT UNSIGNED NOT NULL DEFAULT 1,
            error_message VARCHAR(500) NULL,
            created_at DATETIME NOT NULL,
            sent_at DATETIME NULL,
            KEY idx_notification_user_created (user_id, created_at),
            KEY idx_notification_job (job_id),
            CONSTRAINT fk_notification_job FOREIGN KEY (job_id) REFERENCES analysis_jobs(id) ON DELETE CASCADE,
            CONSTRAINT fk_notification_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS product_feedback (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT UNSIGNED NOT NULL,
            category VARCHAR(32) NOT NULL,
            message VARCHAR(1000) NOT NULL,
            created_at DATETIME NOT NULL,
            KEY idx_feedback_user_created (user_id, created_at),
            CONSTRAINT fk_feedback_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS site_settings (
            setting_key VARCHAR(64) NOT NULL PRIMARY KEY,
            value_json JSON NOT NULL,
            updated_by BIGINT UNSIGNED NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT fk_site_settings_user FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_channel_configs (
            user_id BIGINT UNSIGNED NOT NULL,
            channel VARCHAR(32) NOT NULL,
            config_encrypted BLOB NOT NULL,
            masked_label VARCHAR(191) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (user_id, channel),
            CONSTRAINT fk_channel_config_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS momentum_alert_deliveries (
            user_id BIGINT UNSIGNED NOT NULL,
            symbol VARCHAR(64) NOT NULL,
            last_sent_at DATETIME NOT NULL,
            PRIMARY KEY (user_id, symbol),
            CONSTRAINT fk_momentum_alert_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ]
    with get_conn() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
            for sql in (
                """
                ALTER TABLE email_login_codes
                ADD COLUMN failed_attempts INT UNSIGNED NOT NULL DEFAULT 0
                AFTER consumed_at
                """,
                """
                ALTER TABLE payment_orders
                ADD COLUMN order_kind VARCHAR(32) NOT NULL DEFAULT 'one_time'
                AFTER environment
                """,
                """
                ALTER TABLE payment_orders
                ADD COLUMN plan_code VARCHAR(32) NULL
                AFTER order_kind
                """,
                """
                ALTER TABLE payment_webhook_events
                ADD COLUMN event_timestamp DATETIME(6) NULL
                AFTER event_id
                """,
                """
                ALTER TABLE payment_orders
                ADD COLUMN checkout_url VARCHAR(2048) NULL
                AFTER checkout_session_id
                """,
            ):
                try:
                    cur.execute(sql)
                except pymysql.MySQLError as exc:
                    if exc.args and int(exc.args[0]) == 1060:
                        continue
                    raise
            try:
                cur.execute(
                    """
                    ALTER TABLE payment_webhook_events
                    DROP INDEX uk_payment_webhook_delivery
                    """
                )
            except pymysql.MySQLError as exc:
                if not exc.args or int(exc.args[0]) != 1091:
                    raise
            try:
                cur.execute(
                    """
                    ALTER TABLE payment_webhook_events
                    DROP INDEX uk_payment_webhook_business_event
                    """
                )
            except pymysql.MySQLError as exc:
                if not exc.args or int(exc.args[0]) != 1091:
                    raise
            try:
                cur.execute(
                    """
                    ALTER TABLE payment_webhook_events
                    ADD INDEX idx_payment_webhook_business_event(event_type, event_id)
                    """
                )
            except pymysql.MySQLError as exc:
                if not exc.args or int(exc.args[0]) != 1061:
                    raise
            try:
                cur.execute(
                    """
                    ALTER TABLE payment_webhook_events
                    ADD INDEX idx_payment_webhook_delivery(delivery_id)
                    """
                )
            except pymysql.MySQLError as exc:
                if not exc.args or int(exc.args[0]) != 1061:
                    raise
            try:
                cur.execute(
                    """
                    ALTER TABLE payment_webhook_events
                    ADD UNIQUE INDEX uk_payment_webhook_occurrence(
                        event_type, event_id, event_timestamp
                    )
                    """
                )
            except pymysql.MySQLError as exc:
                if not exc.args or int(exc.args[0]) != 1061:
                    raise

            timestamp_migration_key = "2026-08-07-subscription-event-microseconds"
            cur.execute(
                "SELECT migration_key FROM app_migrations WHERE migration_key = %s",
                (timestamp_migration_key,),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    ALTER TABLE subscriptions
                    MODIFY COLUMN last_event_at DATETIME(6) NOT NULL
                    """
                )
                cur.execute(
                    """
                    INSERT IGNORE INTO app_migrations(migration_key, applied_at)
                    VALUES (%s, %s)
                    """,
                    (timestamp_migration_key, utc_now()),
                )

            migration_key = "2026-08-07-existing-users-pro-14d"
            cur.execute(
                "SELECT migration_key FROM app_migrations WHERE migration_key = %s",
                (migration_key,),
            )
            if not cur.fetchone():
                now = utc_now()
                cur.execute(
                    """
                    INSERT IGNORE INTO entitlement_grants(
                        user_id, grant_code, plan_code, starts_at, ends_at, created_at
                    )
                    SELECT id, 'legacy_launch_14d', 'pro_monthly', %s, %s, %s
                    FROM users
                    """,
                    (now, now + timedelta(days=14), now),
                )
                cur.execute(
                    """
                    INSERT IGNORE INTO app_migrations(migration_key, applied_at)
                    VALUES (%s, %s)
                    """,
                    (migration_key, now),
                )


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _next_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _payment_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    amount = row.get("amount")
    return {
        "id": int(row["id"]),
        "external_id": row["external_id"],
        "status": row["status"],
        "environment": row["environment"],
        "order_kind": row.get("order_kind") or "one_time",
        "plan_code": row.get("plan_code"),
        "store_id": row.get("store_id"),
        "product_id": row.get("product_id"),
        "checkout_session_id": row.get("checkout_session_id"),
        "checkout_url": row.get("checkout_url"),
        "provider_order_id": row.get("provider_order_id"),
        "payment_id": row.get("payment_id"),
        "buyer_email": row["buyer_email"],
        "currency": row.get("currency"),
        "amount": format(amount, "f") if isinstance(amount, Decimal) else (str(amount) if amount else None),
        "expires_at": _iso(row.get("expires_at")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "completed_at": _iso(row.get("completed_at")),
    }


def create_payment_order(
    user_id: int,
    external_id: str,
    buyer_email: str,
    *,
    order_kind: str = "subscription",
    plan_code: str = "pro_monthly",
) -> None:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payment_orders(
                    external_id, user_id, order_kind, plan_code, status,
                    buyer_email, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, 'creating', %s, %s, %s)
                """,
                (
                    external_id[:64],
                    user_id,
                    order_kind[:32],
                    plan_code[:32] or None,
                    buyer_email[:255].lower(),
                    now,
                    now,
                ),
            )


def reserve_subscription_checkout(
    user_id: int,
    external_id: str,
    buyer_email: str,
    *,
    plan_code: str = "pro_monthly",
) -> dict[str, Any]:
    now = utc_now()
    with get_conn() as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE id = %s FOR UPDATE",
                    (user_id,),
                )
                if not cur.fetchone():
                    raise ValueError("用户不存在")

                cur.execute(
                    """
                    SELECT status
                    FROM subscriptions
                    WHERE user_id = %s
                      AND environment = 'test'
                      AND status IN ('active', 'canceling', 'past_due')
                    ORDER BY updated_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                subscription = cur.fetchone()
                if subscription:
                    conn.commit()
                    return {
                        "created": False,
                        "reason": "subscription_exists",
                        "status": str(subscription["status"]),
                    }

                cur.execute(
                    """
                    SELECT *
                    FROM payment_orders
                    WHERE user_id = %s
                      AND environment = 'test'
                      AND order_kind = 'subscription'
                      AND status IN ('creating', 'pending', 'unknown')
                    ORDER BY created_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                existing_order = cur.fetchone()
                if existing_order:
                    conn.commit()
                    return {
                        "created": False,
                        "reason": "checkout_in_progress",
                        "order": _payment_order_payload(existing_order),
                    }

                cur.execute(
                    """
                    INSERT INTO payment_orders(
                        external_id, user_id, order_kind, plan_code, status,
                        buyer_email, created_at, updated_at
                    )
                    VALUES (%s, %s, 'subscription', %s, 'creating', %s, %s, %s)
                    """,
                    (
                        external_id[:64],
                        user_id,
                        plan_code[:32] or None,
                        buyer_email[:255].lower(),
                        now,
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"created": True, "external_id": external_id[:64]}


def set_payment_checkout(
    user_id: int,
    external_id: str,
    checkout: dict[str, Any],
) -> None:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payment_orders
                SET status = 'pending',
                    store_id = %s,
                    product_id = %s,
                    checkout_session_id = %s,
                    checkout_url = %s,
                    currency = %s,
                    amount = %s,
                    expires_at = %s,
                    updated_at = %s
                WHERE external_id = %s AND user_id = %s
                """,
                (
                    str(checkout.get("storeId") or "")[:128] or None,
                    str(checkout.get("productId") or "")[:128] or None,
                    str(checkout.get("sessionId") or "")[:191] or None,
                    str(checkout.get("checkoutUrl") or "")[:2048] or None,
                    str(checkout.get("currency") or "USD")[:3].upper(),
                    Decimal(str(checkout.get("amount") or "19.99")),
                    _parse_utc_datetime(checkout.get("expiresAt")),
                    now,
                    external_id,
                    user_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("支付订单不存在或不属于当前用户")


def mark_payment_order_failed(user_id: int, external_id: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payment_orders
                SET status = 'failed', updated_at = %s
                WHERE external_id = %s AND user_id = %s AND status = 'creating'
                """,
                (utc_now(), external_id, user_id),
            )


def mark_payment_order_unknown(user_id: int, external_id: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payment_orders
                SET status = 'unknown', updated_at = %s
                WHERE external_id = %s AND user_id = %s AND status = 'creating'
                """,
                (utc_now(), external_id, user_id),
            )


def close_unfinished_payment_order(user_id: int, external_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE payment_orders
                SET status = 'failed', updated_at = %s
                WHERE external_id = %s
                  AND user_id = %s
                  AND status IN ('creating', 'pending', 'unknown')
                """,
                (utc_now(), external_id, user_id),
            )
            return cur.rowcount == 1


def get_payment_order(user_id: int, external_id: str | None = None) -> dict[str, Any] | None:
    where = "user_id = %s"
    params: list[Any] = [user_id]
    if external_id:
        where += " AND external_id = %s"
        params.append(external_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, external_id, status, environment, order_kind, plan_code,
                       store_id, product_id,
                       checkout_session_id, checkout_url,
                       provider_order_id, payment_id, buyer_email,
                       currency, amount, expires_at, created_at, updated_at, completed_at
                FROM payment_orders
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
    return _payment_order_payload(row) if row else None


WAFFO_SUBSCRIPTION_EVENTS = {
    "subscription.activated": "active",
    "subscription.payment_succeeded": "active",
    "subscription.canceling": "canceling",
    "subscription.uncanceled": "active",
    "subscription.canceled": "canceled",
    "subscription.past_due": "past_due",
}
WAFFO_EVENT_PRECEDENCE = {
    "subscription.activated": 10,
    "subscription.payment_succeeded": 20,
    "subscription.uncanceled": 20,
    "subscription.past_due": 30,
    "subscription.canceling": 40,
    "subscription.canceled": 50,
}
WAFFO_PRO_PRODUCT_MARKER = "marketbrief-pro-monthly-v1"
PRO_MONTHLY_AMOUNT = Decimal("19.99")


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (ValueError, ArithmeticError):
        return None


def _subscription_payload(row: dict[str, Any]) -> dict[str, Any]:
    amount = row.get("amount")
    return {
        "id": int(row["id"]),
        "provider_subscription_id": row["provider_subscription_id"],
        "checkout_external_id": row.get("checkout_external_id"),
        "plan_code": row["plan_code"],
        "status": row["status"],
        "environment": row["environment"],
        "product_id": row["product_id"],
        "buyer_email": row["buyer_email"],
        "currency": row["currency"],
        "amount": format(amount, "f") if isinstance(amount, Decimal) else str(amount),
        "billing_period": row["billing_period"],
        "current_period_start": _iso(row.get("current_period_start")),
        "current_period_end": _iso(row.get("current_period_end")),
        "grace_until": _iso(row.get("grace_until")),
        "canceled_at": _iso(row.get("canceled_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def process_waffo_subscription_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("mode") != "test":
        raise ValueError("只接受 Waffo test 环境 webhook")
    event_type = str(event.get("eventType") or "").strip()
    next_status = WAFFO_SUBSCRIPTION_EVENTS.get(event_type)
    if not next_status:
        raise ValueError("不支持的 Waffo 订阅 webhook")

    data = event.get("data")
    if not isinstance(data, dict):
        raise ValueError("Waffo webhook 缺少 data")
    metadata = data.get("orderMetadata")
    if not isinstance(metadata, dict):
        metadata = {}
    product_metadata = data.get("productMetadata")
    if not isinstance(product_metadata, dict):
        product_metadata = {}

    delivery_id = str(event.get("id") or "").strip()
    event_id = str(event.get("eventId") or "").strip()
    provider_order_id = str(data.get("orderId") or "").strip()[:128]
    if not delivery_id or not event_id or not provider_order_id:
        raise ValueError("Waffo webhook 缺少 id、eventId 或 orderId")

    external_id = str(
        data.get("orderMerchantExternalId") or metadata.get("internalOrderId") or ""
    ).strip()[:64]
    buyer_identity = str(
        data.get("merchantProvidedBuyerIdentity")
        or data.get("buyerIdentity")
        or ""
    ).strip()[:191]
    buyer_email = str(data.get("buyerEmail") or "").strip().lower()[:255]
    event_product_id = str(
        data.get("productId") or data.get("subscriptionProductId") or ""
    ).strip()[:128]
    store_id = str(event.get("storeId") or "").strip()[:128]
    currency = str(data.get("currency") or "").strip().upper()[:3]
    amount = _decimal_or_none(data.get("amount"))
    billing_period = str(data.get("billingPeriod") or "monthly").strip().lower()[:32]
    now = utc_now()
    event_at = _parse_utc_datetime(event.get("timestamp"))
    if not event_at:
        raise ValueError("Waffo webhook 缺少有效 timestamp")
    period_start = _parse_utc_datetime(data.get("currentPeriodStart"))
    period_end = _parse_utc_datetime(data.get("currentPeriodEnd"))
    canceled_at = _parse_utc_datetime(data.get("canceledAt"))
    payload_json = json.dumps(event, ensure_ascii=False)

    with get_conn() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT IGNORE INTO payment_webhook_events(
                        delivery_id, event_type, event_id, event_timestamp, mode,
                        external_order_id, payload_json, received_at
                    )
                    VALUES (%s, %s, %s, %s, 'test', %s, %s, %s)
                    """,
                    (
                        delivery_id[:191],
                        event_type[:64],
                        event_id[:191],
                        event_at,
                        external_id or None,
                        payload_json,
                        now,
                    ),
                )
                if cur.rowcount == 0:
                    conn.commit()
                    return {
                        "duplicate": True,
                        "linked": bool(external_id or provider_order_id),
                    }
                webhook_event_row_id = int(cur.lastrowid)

                cur.execute(
                    """
                    SELECT *
                    FROM subscriptions
                    WHERE provider = 'waffo'
                      AND environment = 'test'
                      AND provider_subscription_id = %s
                    FOR UPDATE
                    """,
                    (provider_order_id,),
                )
                subscription = cur.fetchone()

                payment_order = None
                if external_id:
                    cur.execute(
                        """
                        SELECT *
                        FROM payment_orders
                        WHERE external_id = %s AND environment = 'test'
                        FOR UPDATE
                        """,
                        (external_id,),
                    )
                    payment_order = cur.fetchone()

                if subscription:
                    user_id = int(subscription["user_id"])
                    expected_product_id = str(subscription["product_id"])
                    expected_email = str(subscription["buyer_email"]).lower()
                elif payment_order:
                    if payment_order.get("order_kind") != "subscription":
                        raise ValueError("Waffo webhook 关联的不是订阅订单")
                    if payment_order.get("plan_code") != "pro_monthly":
                        raise ValueError("Waffo webhook 套餐不匹配")
                    user_id = int(payment_order["user_id"])
                    expected_product_id = str(payment_order.get("product_id") or "")
                    expected_email = str(payment_order["buyer_email"]).lower()
                else:
                    raise ValueError("Waffo webhook 无法关联到本地订阅订单")

                if (
                    subscription
                    and payment_order
                    and int(subscription["user_id"]) != int(payment_order["user_id"])
                ):
                    raise ValueError("Waffo webhook 订单与订阅用户不一致")
                expected_identity = f"marketbrief-user:{user_id}"
                if buyer_identity != expected_identity:
                    raise ValueError("Waffo webhook 用户归属校验失败")
                metadata_user = str(metadata.get("marketbriefUserId") or "").strip()
                if metadata_user and metadata_user != str(user_id):
                    raise ValueError("Waffo webhook metadata 用户归属校验失败")
                if store_id and payment_order and payment_order.get("store_id"):
                    if store_id != str(payment_order["store_id"]):
                        raise ValueError("Waffo webhook Store 不匹配")
                if store_id and subscription and subscription.get("store_id"):
                    if store_id != str(subscription["store_id"]):
                        raise ValueError("Waffo webhook Store 不匹配")
                if event_product_id and expected_product_id and event_product_id != expected_product_id:
                    raise ValueError("Waffo webhook 商品不匹配")
                marker = str(product_metadata.get("marketbriefIntegration") or "").strip()
                if marker and marker != WAFFO_PRO_PRODUCT_MARKER:
                    raise ValueError("Waffo webhook 商品标记不匹配")
                if billing_period != "monthly":
                    raise ValueError("Waffo webhook 计费周期不是 monthly")
                if event_type in {
                    "subscription.activated",
                    "subscription.payment_succeeded",
                }:
                    if currency != "USD" or amount != PRO_MONTHLY_AMOUNT:
                        raise ValueError("Waffo webhook 币种或金额不匹配")

                final_product_id = event_product_id or expected_product_id
                if not final_product_id or not store_id:
                    raise ValueError("Waffo webhook 缺少可信商品或 Store 信息")
                final_email = buyer_email or expected_email
                final_amount = amount or (
                    _decimal_or_none(subscription.get("amount")) if subscription else None
                ) or (
                    _decimal_or_none(payment_order.get("amount")) if payment_order else None
                ) or PRO_MONTHLY_AMOUNT

                if payment_order and event_type in {
                    "subscription.activated",
                    "subscription.payment_succeeded",
                }:
                    cur.execute(
                        """
                        UPDATE payment_orders
                        SET status = 'completed',
                            provider_order_id = %s,
                            payment_id = %s,
                            currency = %s,
                            amount = %s,
                            updated_at = %s,
                            completed_at = %s
                        WHERE id = %s
                        """,
                        (
                            provider_order_id,
                            str(data.get("paymentId") or "")[:128] or None,
                            currency,
                            final_amount,
                            now,
                            event_at,
                            payment_order["id"],
                        ),
                    )

                last_event_at = subscription.get("last_event_at") if subscription else None
                last_event_type = (
                    str(subscription.get("last_event_type") or "")
                    if subscription
                    else ""
                )
                event_is_stale = bool(
                    last_event_at
                    and (
                        last_event_at > event_at
                        or (
                            last_event_at == event_at
                            and WAFFO_EVENT_PRECEDENCE.get(last_event_type, 0)
                            >= WAFFO_EVENT_PRECEDENCE.get(event_type, 0)
                        )
                    )
                )
                if event_is_stale:
                    cur.execute(
                        """
                        UPDATE payment_webhook_events
                        SET processed_at = %s
                        WHERE id = %s
                        """,
                        (now, webhook_event_row_id),
                    )
                    conn.commit()
                    return {
                        "duplicate": False,
                        "linked": True,
                        "ignored_out_of_order": True,
                        "status": subscription["status"],
                    }

                if not period_start:
                    period_start = (
                        subscription.get("current_period_start") if subscription else None
                    ) or event_at
                if not period_end:
                    period_end = (
                        subscription.get("current_period_end") if subscription else None
                    ) or _next_month(period_start)
                if next_status == "active" and period_end <= event_at:
                    period_end = _next_month(event_at)
                grace_until = event_at + timedelta(days=3) if next_status == "past_due" else None
                if next_status == "canceled" and not canceled_at:
                    canceled_at = event_at

                if subscription:
                    cur.execute(
                        """
                        UPDATE subscriptions
                        SET checkout_external_id = COALESCE(%s, checkout_external_id),
                            status = %s,
                            buyer_identity = %s,
                            buyer_email = %s,
                            currency = %s,
                            amount = %s,
                            billing_period = %s,
                            current_period_start = %s,
                            current_period_end = %s,
                            grace_until = %s,
                            canceled_at = %s,
                            last_event_at = %s,
                            last_event_type = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            external_id or None,
                            next_status,
                            buyer_identity,
                            final_email,
                            currency or subscription["currency"],
                            final_amount,
                            billing_period,
                            period_start,
                            period_end,
                            grace_until,
                            canceled_at,
                            event_at,
                            event_type,
                            now,
                            subscription["id"],
                        ),
                    )
                    subscription_id = int(subscription["id"])
                else:
                    cur.execute(
                        """
                        INSERT INTO subscriptions(
                            user_id, provider, environment, provider_subscription_id,
                            checkout_external_id, store_id, product_id, plan_code, status,
                            buyer_identity, buyer_email, currency, amount, billing_period,
                            current_period_start, current_period_end, grace_until, canceled_at,
                            last_event_at, last_event_type, created_at, updated_at
                        )
                        VALUES (
                            %s, 'waffo', 'test', %s, %s, %s, %s, 'pro_monthly', %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            user_id,
                            provider_order_id,
                            external_id or None,
                            store_id,
                            final_product_id,
                            next_status,
                            buyer_identity,
                            final_email,
                            currency or "USD",
                            final_amount,
                            billing_period,
                            period_start,
                            period_end,
                            grace_until,
                            canceled_at,
                            event_at,
                            event_type,
                            now,
                            now,
                        ),
                    )
                    subscription_id = int(cur.lastrowid)

                cur.execute(
                    """
                    UPDATE payment_webhook_events
                    SET processed_at = %s
                    WHERE id = %s
                    """,
                    (now, webhook_event_row_id),
                )
            conn.commit()
            return {
                "duplicate": False,
                "linked": True,
                "subscription_id": subscription_id,
                "external_id": external_id or None,
                "status": next_status,
            }
        except Exception:
            conn.rollback()
            raise


def get_subscription(user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE user_id = %s AND environment = 'test'
                ORDER BY
                  CASE
                    WHEN status IN ('active', 'canceling', 'past_due') THEN 0
                    ELSE 1
                  END,
                  updated_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
    return _subscription_payload(row) if row else None


def get_active_entitlement(user_id: int, at: datetime | None = None) -> dict[str, Any] | None:
    now = at or utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan_code, status, current_period_end, grace_until,
                       provider_subscription_id
                FROM subscriptions
                WHERE user_id = %s
                  AND (
                    (status IN ('active', 'canceling') AND current_period_end > %s)
                    OR (status = 'past_due' AND grace_until > %s)
                  )
                ORDER BY GREATEST(current_period_end, COALESCE(grace_until, current_period_end)) DESC
                LIMIT 1
                """,
                (user_id, now, now),
            )
            subscription = cur.fetchone()
            if subscription:
                active_until = (
                    subscription.get("grace_until")
                    if subscription["status"] == "past_due"
                    else subscription["current_period_end"]
                )
                return {
                    "plan_code": subscription["plan_code"],
                    "status": subscription["status"],
                    "source": "subscription",
                    "active_until": active_until,
                    "provider_subscription_id": subscription["provider_subscription_id"],
                }

            cur.execute(
                """
                SELECT plan_code, grant_code, ends_at
                FROM entitlement_grants
                WHERE user_id = %s AND starts_at <= %s AND ends_at > %s
                ORDER BY ends_at DESC
                LIMIT 1
                """,
                (user_id, now, now),
            )
            grant = cur.fetchone()
    if not grant:
        return None
    return {
        "plan_code": grant["plan_code"],
        "status": "trial",
        "source": grant["grant_code"],
        "active_until": grant["ends_at"],
        "provider_subscription_id": None,
    }


def get_usage_counts(user_id: int, period_keys: dict[str, str]) -> dict[str, int]:
    if not period_keys:
        return {}
    metrics = list(period_keys)
    placeholders = ", ".join(["%s"] * len(metrics))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT metric, period_key, used_count
                FROM usage_counters
                WHERE user_id = %s AND metric IN ({placeholders})
                """,
                (user_id, *metrics),
            )
            rows = cur.fetchall()
    counts = {metric: 0 for metric in metrics}
    for row in rows:
        metric = str(row["metric"])
        if row["period_key"] == period_keys.get(metric):
            counts[metric] = int(row["used_count"])
    return counts


def consume_usage(
    user_id: int,
    metric: str,
    period_key: str,
    limit: int,
    amount: int = 1,
) -> tuple[bool, int]:
    if amount <= 0:
        raise ValueError("用量增量必须大于 0")
    if amount > limit:
        return False, 0
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            params = (
                amount,
                now,
                user_id,
                metric[:64],
                period_key[:16],
                amount,
                limit,
            )
            cur.execute(
                """
                UPDATE usage_counters
                SET used_count = used_count + %s, updated_at = %s
                WHERE user_id = %s AND metric = %s AND period_key = %s
                  AND used_count + %s <= %s
                """,
                params,
            )
            allowed = cur.rowcount == 1
            if not allowed:
                cur.execute(
                    """
                    INSERT IGNORE INTO usage_counters(
                        user_id, metric, period_key, used_count, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        metric[:64],
                        period_key[:16],
                        amount,
                        now,
                    ),
                )
                allowed = cur.rowcount == 1
                if not allowed:
                    cur.execute(
                        """
                        UPDATE usage_counters
                        SET used_count = used_count + %s, updated_at = %s
                        WHERE user_id = %s AND metric = %s AND period_key = %s
                          AND used_count + %s <= %s
                        """,
                        params,
                    )
                    allowed = cur.rowcount == 1
            cur.execute(
                """
                SELECT used_count
                FROM usage_counters
                WHERE user_id = %s AND metric = %s AND period_key = %s
                """,
                (user_id, metric[:64], period_key[:16]),
            )
            row = cur.fetchone()
    return allowed, int(row["used_count"]) if row else 0


def refund_usage(
    user_id: int,
    metric: str,
    period_key: str,
    amount: int = 1,
) -> None:
    if amount <= 0:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE usage_counters
                SET used_count = GREATEST(0, used_count - %s), updated_at = %s
                WHERE user_id = %s AND metric = %s AND period_key = %s
                """,
                (
                    amount,
                    utc_now(),
                    user_id,
                    metric[:64],
                    period_key[:16],
                ),
            )


def save_report(
    user_id: int,
    symbol: str,
    asset_type: str,
    report: dict[str, Any],
    *,
    max_reports: int | None = None,
) -> int:
    created_at = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reports(user_id, symbol, asset_type, stance, summary, report_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    symbol,
                    asset_type,
                    report.get("stance", "neutral"),
                    report.get("summary", ""),
                    json.dumps(report, ensure_ascii=False),
                    created_at,
                ),
            )
            report_id = int(cur.lastrowid)
            if max_reports is not None and max_reports > 0:
                cur.execute(
                    """
                    DELETE FROM reports
                    WHERE user_id = %s
                      AND id NOT IN (
                        SELECT id FROM (
                            SELECT id
                            FROM reports
                            WHERE user_id = %s
                            ORDER BY created_at DESC, id DESC
                            LIMIT %s
                        ) AS retained_reports
                      )
                    """,
                    (user_id, user_id, max_reports),
                )
            return report_id


def list_reports(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, asset_type, stance, summary, report_json, created_at
                FROM reports
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = row["report_json"]
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            report = json.loads(payload)
        else:
            report = payload
        created = row["created_at"]
        result.append(
            {
                "id": int(row["id"]),
                "symbol": row["symbol"],
                "asset_type": row["asset_type"],
                "stance": row["stance"],
                "summary": row["summary"],
                "report": report,
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
            }
        )
    return result


def delete_report(user_id: int, report_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reports WHERE id = %s AND user_id = %s", (report_id, user_id))
            return cur.rowcount > 0


def get_watch_symbols(user_id: int) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol FROM watch_symbols
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            return [str(row["symbol"]) for row in cur.fetchall()]


def set_watch_symbols(
    user_id: int,
    symbols: list[str],
    *,
    max_symbols: int = 30,
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol[:64])
        if len(cleaned) >= max_symbols:
            break
    now = utc_now()
    with get_conn() as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE id = %s FOR UPDATE",
                    (user_id,),
                )
                if not cur.fetchone():
                    raise ValueError("用户不存在")
                cur.execute("DELETE FROM watch_symbols WHERE user_id = %s", (user_id,))
                for symbol in cleaned:
                    cur.execute(
                        """
                        INSERT INTO watch_symbols(user_id, symbol, created_at)
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, symbol, now),
                    )
                if cleaned:
                    placeholders = ", ".join(["%s"] * len(cleaned))
                    cur.execute(
                        f"""
                        DELETE FROM signal_snapshots
                        WHERE user_id = %s
                          AND symbol NOT IN ({placeholders})
                        """,
                        (user_id, *cleaned),
                    )
                    cur.execute(
                        f"""
                        DELETE FROM signal_events
                        WHERE user_id = %s
                          AND symbol NOT IN ({placeholders})
                        """,
                        (user_id, *cleaned),
                    )
                else:
                    cur.execute(
                        "DELETE FROM signal_snapshots WHERE user_id = %s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM signal_events WHERE user_id = %s",
                        (user_id,),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return cleaned


def get_signal_snapshots(user_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.symbol, s.payload_json
                FROM signal_snapshots s
                JOIN watch_symbols w
                  ON w.user_id = s.user_id AND w.symbol = s.symbol
                WHERE s.user_id = %s
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    snapshots: dict[str, Any] = {}
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        snapshots[str(row["symbol"])] = payload
    return snapshots


def upsert_signal_snapshot(user_id: int, symbol: str, payload: dict[str, Any]) -> None:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_snapshots(user_id, symbol, payload_json, updated_at)
                SELECT user_id, symbol, %s, %s
                FROM watch_symbols
                WHERE user_id = %s AND symbol = %s
                ON DUPLICATE KEY UPDATE payload_json = VALUES(payload_json), updated_at = VALUES(updated_at)
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    user_id,
                    symbol[:64],
                ),
            )


def list_signal_events(
    user_id: int,
    limit: int = 80,
    *,
    history_days: int = 90,
) -> list[dict[str, Any]]:
    since = utc_now() - timedelta(days=max(1, history_days))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, symbol, payload_json, created_at
                FROM signal_events
                WHERE user_id = %s AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, since, limit),
            )
            rows = cur.fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            payload = {}
        created = row["created_at"]
        event = dict(payload)
        event["id"] = int(row["id"])
        event["symbol"] = row["symbol"]
        event.setdefault(
            "checked_at",
            created.isoformat() if hasattr(created, "isoformat") else str(created),
        )
        events.append(event)
    return events


def add_signal_event(user_id: int, symbol: str, payload: dict[str, Any]) -> int:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signal_events(user_id, symbol, payload_json, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, symbol[:64], json.dumps(payload, ensure_ascii=False), now),
            )
            return int(cur.lastrowid)


def prune_signal_events(user_id: int, history_days: int) -> int:
    cutoff = utc_now() - timedelta(days=max(1, history_days))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM signal_events WHERE user_id = %s AND created_at < %s",
                (user_id, cutoff),
            )
            return int(cur.rowcount)


def get_brief_preferences(user_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled, delivery_time, timezone, channels_json,
                       only_changes, min_confidence, updated_at
                FROM brief_preferences WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    if not row:
        return {
            "enabled": False,
            "delivery_time": "18:00",
            "timezone": "Asia/Shanghai",
            "channels": ["email"],
            "only_changes": True,
            "min_confidence": 40.0,
            "updated_at": None,
        }
    channels = row.get("channels_json") or []
    if isinstance(channels, str):
        channels = json.loads(channels)
    return {
        "enabled": bool(row["enabled"]),
        "delivery_time": str(row["delivery_time"]),
        "timezone": str(row["timezone"]),
        "channels": [str(item) for item in channels if str(item)],
        "only_changes": bool(row["only_changes"]),
        "min_confidence": float(row["min_confidence"]),
        "updated_at": _iso(row.get("updated_at")),
    }


def save_brief_preferences(user_id: int, preferences: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brief_preferences(
                    user_id, enabled, delivery_time, timezone, channels_json,
                    only_changes, min_confidence, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    enabled = VALUES(enabled),
                    delivery_time = VALUES(delivery_time),
                    timezone = VALUES(timezone),
                    channels_json = VALUES(channels_json),
                    only_changes = VALUES(only_changes),
                    min_confidence = VALUES(min_confidence),
                    updated_at = VALUES(updated_at)
                """,
                (
                    user_id,
                    1 if preferences["enabled"] else 0,
                    preferences["delivery_time"],
                    preferences["timezone"],
                    json.dumps(preferences["channels"], ensure_ascii=False),
                    1 if preferences["only_changes"] else 0,
                    preferences["min_confidence"],
                    now,
                    now,
                ),
            )
    return get_brief_preferences(user_id)


def list_enabled_brief_preferences() -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.user_id, u.email, p.delivery_time, p.timezone,
                       p.channels_json, p.only_changes, p.min_confidence
                FROM brief_preferences p
                JOIN users u ON u.id = p.user_id
                WHERE p.enabled = 1
                ORDER BY p.user_id
                """
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        channels = row.get("channels_json") or []
        if isinstance(channels, str):
            channels = json.loads(channels)
        result.append(
            {
                "user_id": int(row["user_id"]),
                "email": str(row["email"]),
                "delivery_time": str(row["delivery_time"]),
                "timezone": str(row["timezone"]),
                "channels": [str(item) for item in channels],
                "only_changes": bool(row["only_changes"]),
                "min_confidence": float(row["min_confidence"]),
            }
        )
    return result


def create_analysis_job(user_id: int, local_date: str, symbols: list[str]) -> int | None:
    now = utc_now()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analysis_jobs(
                        user_id, local_date, status, symbols_json, started_at
                    ) VALUES (%s, %s, 'running', %s, %s)
                    """,
                    (user_id, local_date, json.dumps(symbols), now),
                )
                return int(cur.lastrowid)
    except pymysql.IntegrityError as exc:
        if exc.args and int(exc.args[0]) == 1062:
            return None
        raise


def complete_analysis_job(job_id: int, brief: dict[str, Any]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_jobs
                SET status = 'completed', brief_json = %s, completed_at = %s,
                    error_message = NULL
                WHERE id = %s
                """,
                (json.dumps(brief, ensure_ascii=False), utc_now(), job_id),
            )


def fail_analysis_job(job_id: int, error: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_jobs
                SET status = 'failed', error_message = %s, completed_at = %s
                WHERE id = %s
                """,
                (error[:500], utc_now(), job_id),
            )


def record_notification_delivery(
    job_id: int,
    user_id: int,
    channel: str,
    *,
    status: str,
    error: str = "",
    attempt_count: int = 1,
) -> int:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_deliveries(
                    job_id, user_id, channel, status, attempt_count, error_message,
                    created_at, sent_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    user_id,
                    channel[:32],
                    status[:24],
                    max(1, int(attempt_count)),
                    error[:500] or None,
                    now,
                    now if status == "sent" else None,
                ),
            )
            return int(cur.lastrowid)


def list_notification_deliveries(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_id, channel, status, attempt_count,
                       error_message, created_at, sent_at
                FROM notification_deliveries
                WHERE user_id = %s
                ORDER BY id DESC LIMIT %s
                """,
                (user_id, max(1, min(int(limit), 100))),
            )
            rows = cur.fetchall()
    return [
        {
            "id": int(row["id"]),
            "job_id": int(row["job_id"]),
            "channel": row["channel"],
            "status": row["status"],
            "attempt_count": int(row["attempt_count"]),
            "error": row.get("error_message") or "",
            "created_at": _iso(row.get("created_at")),
            "sent_at": _iso(row.get("sent_at")),
        }
        for row in rows
    ]


def list_analysis_jobs(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.id, j.local_date, j.status, j.symbols_json,
                       j.error_message, j.started_at, j.completed_at,
                       COUNT(d.id) AS delivery_count,
                       SUM(d.status = 'sent') AS sent_count,
                       SUM(d.status = 'failed') AS failed_count
                FROM analysis_jobs j
                LEFT JOIN notification_deliveries d ON d.job_id = j.id
                WHERE j.user_id = %s
                GROUP BY j.id
                ORDER BY j.local_date DESC, j.id DESC
                LIMIT %s
                """,
                (user_id, max(1, min(int(limit), 100))),
            )
            rows = cur.fetchall()
    result = []
    for row in rows:
        symbols = row.get("symbols_json") or []
        if isinstance(symbols, str):
            symbols = json.loads(symbols)
        result.append(
            {
                "id": int(row["id"]),
                "local_date": str(row["local_date"]),
                "status": row["status"],
                "symbols": symbols,
                "error": row.get("error_message") or "",
                "started_at": _iso(row.get("started_at")),
                "completed_at": _iso(row.get("completed_at")),
                "delivery_count": int(row.get("delivery_count") or 0),
                "sent_count": int(row.get("sent_count") or 0),
                "failed_count": int(row.get("failed_count") or 0),
            }
        )
    return result


def claim_failed_delivery(user_id: int, delivery_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notification_deliveries
                SET status = 'retrying'
                WHERE id = %s AND user_id = %s AND status = 'failed'
                """,
                (delivery_id, user_id),
            )
            if cur.rowcount != 1:
                return None
            cur.execute(
                """
                SELECT d.id, d.job_id, d.channel, d.attempt_count,
                       j.local_date, j.brief_json
                FROM notification_deliveries d
                JOIN analysis_jobs j ON j.id = d.job_id
                WHERE d.id = %s AND d.user_id = %s AND d.status = 'retrying'
                """,
                (delivery_id, user_id),
            )
            row = cur.fetchone()
    if not row:
        return None
    brief = row.get("brief_json") or {}
    if isinstance(brief, str):
        brief = json.loads(brief)
    return {
        "id": int(row["id"]),
        "job_id": int(row["job_id"]),
        "channel": row["channel"],
        "attempt_count": int(row.get("attempt_count") or 1),
        "local_date": str(row["local_date"]),
        "brief": brief,
    }


def update_notification_delivery(
    delivery_id: int,
    user_id: int,
    *,
    status: str,
    attempt_count: int,
    error: str = "",
) -> None:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notification_deliveries
                SET status = %s, attempt_count = %s, error_message = %s,
                    sent_at = %s
                WHERE id = %s AND user_id = %s
                """,
                (
                    status[:24],
                    max(1, int(attempt_count)),
                    error[:500] or None,
                    now if status == "sent" else None,
                    delivery_id,
                    user_id,
                ),
            )


def save_product_feedback(user_id: int, category: str, message: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product_feedback(user_id, category, message, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, category[:32], message[:1000], utc_now()),
            )
            return int(cur.lastrowid)


def get_site_setting(key: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value_json FROM site_settings WHERE setting_key = %s", (key[:64],))
            row = cur.fetchone()
    if not row:
        return None
    value = row.get("value_json")
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def save_site_setting(key: str, value: dict[str, Any], user_id: int) -> None:
    now = utc_now()
    raw = json.dumps(value, ensure_ascii=False)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO site_settings(setting_key, value_json, updated_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE value_json = VALUES(value_json),
                    updated_by = VALUES(updated_by), updated_at = VALUES(updated_at)
                """,
                (key[:64], raw, user_id, now, now),
            )


def save_notification_channel_config(
    user_id: int, channel: str, config_encrypted: bytes, masked_label: str
) -> None:
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_channel_configs(
                    user_id, channel, config_encrypted, masked_label, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE config_encrypted = VALUES(config_encrypted),
                    masked_label = VALUES(masked_label), updated_at = VALUES(updated_at)
                """,
                (user_id, channel[:32], config_encrypted, masked_label[:191], now, now),
            )


def get_notification_channel_configs(user_id: int) -> dict[str, dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT channel, config_encrypted, masked_label, updated_at
                FROM notification_channel_configs WHERE user_id = %s
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return {
        str(row["channel"]): {
            "encrypted": row["config_encrypted"],
            "masked_label": row["masked_label"],
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in rows
    }


def list_notification_channel_configs_by_channel(channel: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id, config_encrypted, masked_label, updated_at
                   FROM notification_channel_configs WHERE channel = %s ORDER BY user_id""",
                (channel,),
            )
            rows = cur.fetchall()
    return [{
        "user_id": int(row["user_id"]),
        "encrypted": row["config_encrypted"],
        "masked_label": row["masked_label"],
        "updated_at": _iso(row.get("updated_at")),
    } for row in rows]


def claim_momentum_alert(user_id: int, symbol: str, cooldown_seconds: int) -> bool:
    """Atomically reserve an alert delivery across processes and restarts."""
    now = utc_now()
    cutoff = now - timedelta(seconds=max(0, cooldown_seconds))
    with get_conn() as conn:
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT last_sent_at FROM momentum_alert_deliveries
                       WHERE user_id = %s AND symbol = %s FOR UPDATE""",
                    (user_id, symbol[:64]),
                )
                row = cur.fetchone()
                if row and row["last_sent_at"] > cutoff:
                    conn.rollback()
                    return False
                cur.execute(
                    """INSERT INTO momentum_alert_deliveries(user_id, symbol, last_sent_at)
                       VALUES (%s, %s, %s)
                       ON DUPLICATE KEY UPDATE last_sent_at = VALUES(last_sent_at)""",
                    (user_id, symbol[:64], now),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def release_momentum_alert_claim(user_id: int, symbol: str) -> None:
    """Release a reservation after delivery failed so the next scan can retry."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM momentum_alert_deliveries WHERE user_id = %s AND symbol = %s",
                (user_id, symbol[:64]),
            )


def delete_notification_channel_config(user_id: int, channel: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM notification_channel_configs WHERE user_id = %s AND channel = %s",
                (user_id, channel),
            )
            return cur.rowcount == 1
