from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

import db
import entitlements
import server as app_server


@contextmanager
def running_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class PaymentRouteTests(unittest.TestCase):
    def test_checkout_requires_login(self):
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=None),
            mock.patch.object(app_server.waffo_bridge, "create_checkout") as create_checkout,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/checkout",
                method="POST",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "未登录")
        create_checkout.assert_not_called()

    def test_login_is_not_blocked_by_local_browser_origin_rewriting(self):
        user = {"id": 7, "email": "local@example.com"}
        with (
            mock.patch.object(app_server.auth, "verify_login_code", return_value=(user, "token")),
            mock.patch.object(app_server.entitlements, "entitlement_snapshot", return_value={}),
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/auth/login",
                method="POST",
                body=b'{"email":"local@example.com","code":"123456"}',
                headers={"Content-Type": "application/json", "Origin": "null"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["email"], "loc***@example.com")

    def test_checkout_binds_logged_in_user_to_internal_order(self):
        checkout = {
            "storeId": "STO_test",
            "productId": "PROD_test",
            "sessionId": "cs_test",
            "checkoutUrl": "https://checkout.waffo.ai/test",
            "expiresAt": "2026-08-07T08:00:00.000Z",
        }
        user = {"id": 23, "email": "buyer@example.com"}
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=user),
            mock.patch.object(
                app_server.db,
                "reserve_subscription_checkout",
                return_value={"created": True},
            ) as reserve,
            mock.patch.object(app_server.db, "set_payment_checkout") as set_checkout,
            mock.patch.object(app_server.waffo_bridge, "create_checkout", return_value=checkout) as create,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/checkout",
                method="POST",
                body=b'{"locale":"en"}',
                headers={"Content-Type": "application/json", "Origin": base_url},
            )

        self.assertEqual(status, 200)
        external_id = payload["order"]
        reserve.assert_called_once_with(
            23,
            external_id,
            "buyer@example.com",
            plan_code="pro_monthly",
        )
        sidecar_payload = create.call_args.args[0]
        self.assertEqual(sidecar_payload["userId"], 23)
        self.assertEqual(sidecar_payload["buyerEmail"], "buyer@example.com")
        self.assertEqual(sidecar_payload["internalOrderId"], external_id)
        self.assertIn(f"payment={external_id}", sidecar_payload["successUrl"])
        set_checkout.assert_called_once_with(23, external_id, checkout)

    def test_checkout_reuses_in_progress_reservation(self):
        user = {"id": 23, "email": "buyer@example.com"}
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=user),
            mock.patch.object(
                app_server.db,
                "reserve_subscription_checkout",
                return_value={
                    "created": False,
                    "reason": "checkout_in_progress",
                    "order": {
                        "external_id": "existing-order",
                        "status": "creating",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            ),
            mock.patch.object(app_server.waffo_bridge, "create_checkout") as create,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/checkout",
                method="POST",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "checkout_in_progress")
        self.assertEqual(payload["order"], "existing-order")
        create.assert_not_called()

    def test_checkout_returns_existing_resumable_url(self):
        user = {"id": 23, "email": "buyer@example.com"}
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=user),
            mock.patch.object(
                app_server.db,
                "reserve_subscription_checkout",
                return_value={
                    "created": False,
                    "reason": "checkout_in_progress",
                    "order": {
                        "external_id": "existing-order",
                        "status": "pending",
                        "checkout_url": "https://checkout.example/resume",
                        "expires_at": expires_at,
                        "amount": "19.99",
                        "currency": "USD",
                    },
                },
            ),
            mock.patch.object(app_server.waffo_bridge, "create_checkout") as create,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/checkout",
                method="POST",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["resumed"])
        self.assertEqual(payload["checkoutUrl"], "https://checkout.example/resume")
        create.assert_not_called()

    def test_unrecoverable_pending_checkout_is_canceled_before_replacement(self):
        user = {"id": 23, "email": "buyer@example.com"}
        checkout = {
            "storeId": "STO_test",
            "productId": "PROD_test",
            "sessionId": "cs_replacement",
            "checkoutUrl": "https://checkout.example/replacement",
            "expiresAt": "2026-08-08T08:00:00.000Z",
        }
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=user),
            mock.patch.object(
                app_server.db,
                "reserve_subscription_checkout",
                side_effect=[
                    {
                        "created": False,
                        "reason": "checkout_in_progress",
                        "order": {
                            "external_id": "unknown-order",
                            "status": "unknown",
                            "updated_at": "2026-08-07T00:00:00",
                        },
                    },
                    {"created": True},
                ],
            ),
            mock.patch.object(
                app_server.waffo_bridge,
                "lookup_subscription_order",
                return_value={"id": "ORD_pending", "status": "pending"},
            ),
            mock.patch.object(
                app_server.waffo_bridge,
                "cancel_subscription",
                return_value={"orderId": "ORD_pending", "status": "canceled"},
            ) as cancel,
            mock.patch.object(
                app_server.db,
                "close_unfinished_payment_order",
                return_value=True,
            ) as close,
            mock.patch.object(app_server.db, "set_payment_checkout"),
            mock.patch.object(
                app_server.waffo_bridge,
                "create_checkout",
                return_value=checkout,
            ),
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/checkout",
                method="POST",
                body=b"{}",
                headers={"Content-Type": "application/json", "Origin": base_url},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["checkoutUrl"], "https://checkout.example/replacement")
        cancel.assert_called_once_with("ORD_pending")
        close.assert_called_once_with(23, "unknown-order")

    def test_webhook_forwards_exact_raw_body_and_signature(self):
        raw = b'{ "eventType": "subscription.activated", "spacing": true }\n'
        event = {
            "id": "delivery-1",
            "eventId": "PAY_1",
            "eventType": "subscription.activated",
            "mode": "test",
            "data": {"orderId": "ORD_1"},
        }
        with (
            mock.patch.object(app_server.waffo_bridge, "verify_webhook", return_value=event) as verify,
            mock.patch.object(
                app_server.db,
                "process_waffo_subscription_event",
                return_value={"duplicate": False, "linked": True},
            ) as process,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/webhook",
                method="POST",
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Waffo-Signature": "t=123,v1=signed",
                },
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["received"])
        verify.assert_called_once_with(raw, "t=123,v1=signed")
        process.assert_called_once_with(event)

    def test_verified_non_test_event_is_not_processed(self):
        event = {
            "id": "delivery-prod",
            "eventId": "PAY_prod",
            "eventType": "subscription.activated",
            "mode": "prod",
            "data": {"orderId": "ORD_prod"},
        }
        with (
            mock.patch.object(app_server.waffo_bridge, "verify_webhook", return_value=event),
            mock.patch.object(app_server.db, "process_waffo_subscription_event") as process,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/webhook",
                method="POST",
                body=b"{}",
                headers={"X-Waffo-Signature": "valid"},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ignored"])
        process.assert_not_called()

    def test_status_lookup_is_scoped_to_current_user(self):
        user = {"id": 99, "email": "owner@example.com"}
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=user),
            mock.patch.object(app_server.db, "get_payment_order", return_value=None) as lookup,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/payments/status?order=order-for-user",
            )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["order"])
        lookup.assert_called_once_with(99, "order-for-user")


class EntitlementRouteTests(unittest.TestCase):
    user = {"id": 31, "email": "quota@example.com"}

    def test_analyze_quota_cannot_be_bypassed_via_direct_api(self):
        error = entitlements.EntitlementError(
            "quota reached",
            code="quota_exceeded",
            status=429,
            metric="analysis_daily",
            limit=10,
            used=10,
        )
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "consume_or_raise",
                side_effect=error,
            ),
            mock.patch.object(app_server, "build_analysis") as build,
            running_server() as base_url,
        ):
            status, payload = request_json(f"{base_url}/api/analyze?symbol=AAPL")

        self.assertEqual(status, 429)
        self.assertEqual(payload["code"], "quota_exceeded")
        self.assertEqual(payload["metric"], "analysis_daily")
        build.assert_not_called()

    def test_failed_market_data_request_refunds_analysis_quota(self):
        usage = {
            "allowed": True,
            "metric": "analysis_daily",
            "period_key": "2026-08-07",
            "used": 1,
            "limit": 10,
        }
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "consume_or_raise",
                return_value=usage,
            ),
            mock.patch.object(
                app_server,
                "build_analysis",
                side_effect=RuntimeError("market data unavailable"),
            ),
            mock.patch.object(app_server.entitlements, "refund") as refund,
            running_server() as base_url,
        ):
            status, _payload = request_json(f"{base_url}/api/analyze?symbol=AAPL")

        self.assertEqual(status, 500)
        refund.assert_called_once_with(
            31,
            "analysis_daily",
            period_key="2026-08-07",
        )

    def test_report_cannot_build_analysis_after_daily_quota_is_exhausted(self):
        error = entitlements.EntitlementError(
            "quota reached",
            code="quota_exceeded",
            status=429,
            metric="analysis_daily",
            limit=10,
            used=10,
        )
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "consume_or_raise",
                side_effect=error,
            ),
            mock.patch.object(app_server, "build_analysis") as build,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/report",
                method="POST",
                body=json.dumps({"symbol": "AAPL"}).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 429)
        self.assertEqual(payload["metric"], "analysis_daily")
        build.assert_not_called()

    def test_scan_symbol_limit_is_enforced_before_work_starts(self):
        error = entitlements.EntitlementError(
            "too many symbols",
            code="quota_exceeded",
            status=429,
            metric="scan_symbols",
            limit=5,
            used=6,
        )
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "ensure_count_within_limit",
                side_effect=error,
            ),
            mock.patch.object(app_server, "scan_symbols") as scan,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/scan",
                method="POST",
                body=json.dumps({"symbols": ["A", "B", "C", "D", "E", "F"]}).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 429)
        self.assertEqual(payload["metric"], "scan_symbols")
        scan.assert_not_called()

    def test_watchlist_limit_is_enforced_server_side(self):
        error = entitlements.EntitlementError(
            "too many symbols",
            code="quota_exceeded",
            status=429,
            metric="watchlist_symbols",
            limit=5,
            used=6,
        )
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "ensure_count_within_limit",
                side_effect=error,
            ),
            mock.patch.object(app_server.db, "set_watch_symbols") as save,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/watchlist",
                method="PUT",
                body=json.dumps({"symbols": ["A", "B", "C", "D", "E", "F"]}).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 429)
        self.assertEqual(payload["metric"], "watchlist_symbols")
        save.assert_not_called()

    def test_ai_quota_exhaustion_falls_back_to_local_report(self):
        usage = {
            "allowed": False,
            "plan": "free",
            "metric": "ai_report_monthly",
            "used": 5,
            "limit": 5,
            "remaining": 0,
            "period": "month",
        }
        local = {"source": "local_rules", "stance": "neutral", "summary": "fallback"}
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server,
                "get_ai_config",
                return_value={"configured": True, "provider": "openai", "model": "test"},
            ),
            mock.patch.object(app_server.entitlements, "try_consume", return_value=usage),
            mock.patch.object(app_server.entitlements, "limit_for", return_value=10),
            mock.patch.object(app_server, "local_report", return_value=local),
            mock.patch.object(app_server, "call_ai_report") as call_ai,
            mock.patch.object(app_server.db, "save_report", return_value=77),
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/report",
                method="POST",
                body=json.dumps(
                    {
                        "symbol": "AAPL",
                        "analysis": {"asset_type": "us_stock"},
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ai"]["quota_exceeded"])
        self.assertEqual(payload["report"]["source"], "local_rules")
        call_ai.assert_not_called()

    def test_ai_provider_failure_refunds_monthly_quota(self):
        usage = {
            "allowed": True,
            "plan": "free",
            "metric": "ai_report_monthly",
            "period_key": "2026-08",
            "used": 1,
            "limit": 5,
            "remaining": 4,
        }
        local = {"source": "local_rules", "stance": "neutral", "summary": "fallback"}
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server,
                "get_ai_config",
                return_value={"configured": True, "provider": "openai", "model": "test"},
            ),
            mock.patch.object(app_server.entitlements, "try_consume", return_value=usage),
            mock.patch.object(app_server.entitlements, "limit_for", return_value=10),
            mock.patch.object(
                app_server,
                "call_ai_report",
                return_value=(
                    None,
                    {
                        "configured": True,
                        "used": False,
                        "provider": "openai",
                        "model": "test",
                        "error": "provider unavailable",
                    },
                ),
            ),
            mock.patch.object(app_server, "local_report", return_value=local),
            mock.patch.object(app_server.db, "save_report", return_value=78),
            mock.patch.object(app_server.entitlements, "refund") as refund,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/report",
                method="POST",
                body=json.dumps(
                    {
                        "symbol": "AAPL",
                        "analysis": {"asset_type": "us_stock"},
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        self.assertIsNone(payload["usage"])
        refund.assert_called_once_with(
            31,
            "ai_report_monthly",
            period_key="2026-08",
        )

    def test_scan_refunds_quota_when_all_symbols_fail(self):
        usage = {
            "allowed": True,
            "metric": "scan_daily",
            "period_key": "2026-08-07",
            "used": 1,
            "limit": 1,
        }
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "ensure_count_within_limit",
                return_value=5,
            ),
            mock.patch.object(
                app_server.entitlements,
                "consume_or_raise",
                return_value=usage,
            ),
            mock.patch.object(
                app_server,
                "scan_symbols",
                return_value=[{"symbol": "AAPL", "error": "market unavailable"}],
            ),
            mock.patch.object(app_server.entitlements, "refund") as refund,
            running_server() as base_url,
        ):
            status, payload = request_json(
                f"{base_url}/api/scan",
                method="POST",
                body=json.dumps({"symbols": ["AAPL"]}).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 502)
        self.assertIn("未计入额度", payload["error"])
        refund.assert_called_once_with(
            31,
            "scan_daily",
            period_key="2026-08-07",
        )

    def test_signal_snapshots_are_filtered_to_current_plan_watchlist(self):
        with (
            mock.patch.object(app_server.auth, "current_user", return_value=self.user),
            mock.patch.object(
                app_server.entitlements,
                "limit_for",
                side_effect=lambda _user_id, metric: {
                    "signal_history_days": 7,
                    "watchlist_symbols": 1,
                }[metric],
            ),
            mock.patch.object(
                app_server.db,
                "get_watch_symbols",
                return_value=["AAPL", "MSFT"],
            ),
            mock.patch.object(
                app_server.db,
                "get_signal_snapshots",
                return_value={
                    "AAPL": {"overall": "bullish"},
                    "MSFT": {"overall": "neutral"},
                },
            ),
            mock.patch.object(
                app_server.db,
                "list_signal_events",
                return_value=[
                    {"id": 1, "symbol": "AAPL", "current_label": "买入观察"},
                    {"id": 2, "symbol": "MSFT", "current_label": "持有观察"},
                    {"id": 3, "symbol": "SKK", "current_label": "卖出/回避"},
                ],
            ),
            running_server() as base_url,
        ):
            status, payload = request_json(f"{base_url}/api/signal-events")

        self.assertEqual(status, 200)
        self.assertEqual(set(payload["snapshots"]), {"AAPL"})
        self.assertEqual([event["symbol"] for event in payload["events"]], ["AAPL"])


class PaymentPersistenceTests(unittest.TestCase):
    def test_persistence_rejects_non_test_events(self):
        with self.assertRaisesRegex(ValueError, "test 环境"):
            db.process_waffo_subscription_event(
                {
                    "id": "delivery-prod",
                    "eventId": "PAY_prod",
                    "eventType": "subscription.activated",
                    "mode": "prod",
                    "data": {"orderId": "ORD_prod"},
                }
            )

    def test_persistence_rejects_non_subscription_events(self):
        with self.assertRaisesRegex(ValueError, "不支持"):
            db.process_waffo_subscription_event(
                {
                    "id": "delivery-one-time",
                    "eventId": "PAY_one_time",
                    "eventType": "order.completed",
                    "mode": "test",
                    "data": {"orderId": "ORD_one_time"},
                }
            )


class EntitlementPolicyTests(unittest.TestCase):
    def test_free_and_pro_limits_are_exposed_from_one_policy(self):
        with (
            mock.patch.object(entitlements.db, "get_active_entitlement", return_value=None),
            mock.patch.object(entitlements.db, "get_usage_counts", return_value={}),
            mock.patch.object(entitlements.db, "get_subscription", return_value=None),
        ):
            free = entitlements.entitlement_snapshot(1)
        self.assertEqual(free["plan"], "free")
        self.assertEqual(free["limits"]["analysis_daily"], 10)
        self.assertEqual(free["limits"]["ai_report_monthly"], 5)
        self.assertEqual(free["limits"]["watchlist_symbols"], 5)

        source = {
            "plan_code": "pro_monthly",
            "status": "active",
            "source": "subscription",
            "active_until": datetime(2026, 9, 1),
        }
        with (
            mock.patch.object(
                entitlements.db,
                "get_active_entitlement",
                return_value=source,
            ),
            mock.patch.object(entitlements.db, "get_usage_counts", return_value={}),
            mock.patch.object(entitlements.db, "get_subscription", return_value=None),
        ):
            pro = entitlements.entitlement_snapshot(1)
        self.assertEqual(pro["plan"], "pro")
        self.assertEqual(pro["limits"]["analysis_daily"], 200)
        self.assertEqual(pro["limits"]["ai_report_monthly"], 100)
        self.assertEqual(pro["limits"]["watchlist_symbols"], 30)

    def test_usage_consumption_uses_plan_specific_limit(self):
        with (
            mock.patch.object(
                entitlements.db,
                "get_active_entitlement",
                return_value=None,
            ),
            mock.patch.object(
                entitlements.db,
                "consume_usage",
                return_value=(True, 1),
            ) as consume,
        ):
            result = entitlements.try_consume(7, "analysis_daily")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["limit"], 10)
        self.assertEqual(consume.call_args.args[3], 10)


@unittest.skipUnless(
    os.environ.get("RUN_MYSQL_INTEGRATION") == "1",
    "set RUN_MYSQL_INTEGRATION=1 to run MySQL lifecycle tests",
)
class MySQLSubscriptionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_server.load_env_file()
        db.ensure_schema()

    def setUp(self):
        self.email = f"marketbrief-test-{uuid.uuid4().hex}@example.com"
        now = db.utc_now()
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users(email, created_at, last_login_at) VALUES (%s, %s, %s)",
                    (self.email, now, now),
                )
                self.user_id = int(cur.lastrowid)

    def tearDown(self):
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (self.user_id,))

    def _prepare_checkout(self):
        self.external_id = str(uuid.uuid4())
        self.provider_order_id = f"ORD_{uuid.uuid4().hex}"
        self.product_id = f"PROD_{uuid.uuid4().hex}"
        self.store_id = f"STO_{uuid.uuid4().hex}"
        db.create_payment_order(
            self.user_id,
            self.external_id,
            self.email,
            order_kind="subscription",
            plan_code="pro_monthly",
        )
        db.set_payment_checkout(
            self.user_id,
            self.external_id,
            {
                "storeId": self.store_id,
                "productId": self.product_id,
                "sessionId": f"cs_{uuid.uuid4().hex}",
                "amount": "19.99",
                "currency": "USD",
                "expiresAt": "2026-08-08T00:00:00.000Z",
            },
        )

    def _event(self, event_type, at, **overrides):
        data = {
            "orderId": self.provider_order_id,
            "orderMerchantExternalId": self.external_id,
            "merchantProvidedBuyerIdentity": f"marketbrief-user:{self.user_id}",
            "buyerEmail": self.email,
            "productId": self.product_id,
            "currency": "USD",
            "amount": "19.99",
            "billingPeriod": "monthly",
            "currentPeriodStart": at.isoformat().replace("+00:00", "Z"),
            "currentPeriodEnd": (at + timedelta(days=30)).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "orderMetadata": {
                "internalOrderId": self.external_id,
                "marketbriefUserId": str(self.user_id),
            },
            "productMetadata": {
                "marketbriefIntegration": "marketbrief-pro-monthly-v1",
            },
        }
        data.update(overrides.pop("data", {}))
        return {
            "id": self.provider_order_id,
            "eventId": self.provider_order_id,
            "eventType": event_type,
            "mode": "test",
            "timestamp": at.isoformat().replace("+00:00", "Z"),
            "storeId": self.store_id,
            "data": data,
            **overrides,
        }

    def test_full_subscription_state_machine_and_out_of_order_events(self):
        self._prepare_checkout()
        started = datetime.now(timezone.utc).replace(microsecond=0)
        activated = self._event("subscription.activated", started)

        result = db.process_waffo_subscription_event(activated)
        self.assertEqual(result["status"], "active")
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "active")
        self.assertIsNone(
            db.get_active_entitlement(
                self.user_id,
                at=(started + timedelta(days=31)).replace(tzinfo=None),
            )
        )

        duplicate = db.process_waffo_subscription_event(activated)
        self.assertTrue(duplicate["duplicate"])

        activation_processed_at = (started - timedelta(days=1)).replace(tzinfo=None)
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE payment_webhook_events
                    SET processed_at = %s
                    WHERE event_type = 'subscription.activated'
                      AND event_id = %s
                    """,
                    (activation_processed_at, self.provider_order_id),
                )

        canceling = self._event(
            "subscription.canceling",
            started + timedelta(minutes=5),
        )
        db.process_waffo_subscription_event(canceling)
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "canceling")
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_type, processed_at
                    FROM payment_webhook_events
                    WHERE event_id = %s
                      AND event_type IN (
                        'subscription.activated',
                        'subscription.canceling'
                      )
                    """,
                    (self.provider_order_id,),
                )
                processed_times = {
                    str(row["event_type"]): row["processed_at"]
                    for row in cur.fetchall()
                }
        self.assertEqual(
            processed_times["subscription.activated"],
            activation_processed_at,
        )
        self.assertIsNotNone(processed_times["subscription.canceling"])

        stale_canceled = self._event(
            "subscription.canceled",
            started + timedelta(minutes=2),
        )
        stale_result = db.process_waffo_subscription_event(stale_canceled)
        self.assertTrue(stale_result["ignored_out_of_order"])
        self.assertEqual(db.get_subscription(self.user_id)["status"], "canceling")

        uncanceled = self._event(
            "subscription.uncanceled",
            started + timedelta(minutes=6),
        )
        db.process_waffo_subscription_event(uncanceled)
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "active")

        repeated_canceling = self._event(
            "subscription.canceling",
            started + timedelta(minutes=7),
            id=canceling["id"],
            eventId=canceling["eventId"],
        )
        repeated_result = db.process_waffo_subscription_event(repeated_canceling)
        self.assertFalse(repeated_result["duplicate"])
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "canceling")

        second_uncanceled = self._event(
            "subscription.uncanceled",
            started + timedelta(minutes=8),
        )
        db.process_waffo_subscription_event(second_uncanceled)

        past_due = self._event(
            "subscription.past_due",
            started + timedelta(minutes=9),
        )
        db.process_waffo_subscription_event(past_due)
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "past_due")

        renewed = self._event(
            "subscription.payment_succeeded",
            started + timedelta(minutes=10),
        )
        db.process_waffo_subscription_event(renewed)
        self.assertEqual(db.get_active_entitlement(self.user_id)["status"], "active")

        canceled = self._event(
            "subscription.canceled",
            started + timedelta(minutes=11),
            data={
                "canceledAt": (started + timedelta(minutes=11))
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        db.process_waffo_subscription_event(canceled)
        self.assertIsNone(db.get_active_entitlement(self.user_id))
        self.assertEqual(db.get_subscription(self.user_id)["status"], "canceled")

    def test_webhook_user_ownership_is_verified(self):
        self._prepare_checkout()
        event = self._event(
            "subscription.activated",
            datetime.now(timezone.utc).replace(microsecond=0),
            data={"merchantProvidedBuyerIdentity": "marketbrief-user:999999"},
        )
        with self.assertRaisesRegex(ValueError, "用户归属"):
            db.process_waffo_subscription_event(event)

    def test_checkout_email_edit_does_not_break_subscription_ownership(self):
        self._prepare_checkout()
        edited_email = f"edited-{uuid.uuid4().hex}@example.com"
        event = self._event(
            "subscription.activated",
            datetime.now(timezone.utc).replace(microsecond=123456),
            data={"buyerEmail": edited_email},
        )

        result = db.process_waffo_subscription_event(event)

        self.assertEqual(result["status"], "active")
        self.assertEqual(
            db.get_subscription(self.user_id)["buyer_email"],
            edited_email,
        )

    def test_same_timestamp_uses_deterministic_event_precedence(self):
        self._prepare_checkout()
        event_at = datetime.now(timezone.utc).replace(microsecond=654321)
        canceling = self._event("subscription.canceling", event_at)
        activated = self._event("subscription.activated", event_at)

        db.process_waffo_subscription_event(canceling)
        stale = db.process_waffo_subscription_event(activated)

        self.assertTrue(stale["ignored_out_of_order"])
        self.assertEqual(db.get_subscription(self.user_id)["status"], "canceling")
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_event_at FROM subscriptions WHERE user_id = %s",
                    (self.user_id,),
                )
                self.assertEqual(cur.fetchone()["last_event_at"], event_at.replace(tzinfo=None))

    def test_stale_success_still_completes_local_payment_order(self):
        self._prepare_checkout()
        started = datetime.now(timezone.utc).replace(microsecond=0)
        db.process_waffo_subscription_event(
            self._event(
                "subscription.canceling",
                started + timedelta(minutes=1),
            )
        )

        stale = db.process_waffo_subscription_event(
            self._event("subscription.activated", started)
        )

        self.assertTrue(stale["ignored_out_of_order"])
        self.assertEqual(db.get_subscription(self.user_id)["status"], "canceling")
        self.assertEqual(
            db.get_payment_order(self.user_id, self.external_id)["status"],
            "completed",
        )

    def test_subscription_checkout_reservation_is_atomic(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def reserve_once(index):
            barrier.wait(timeout=5)
            return db.reserve_subscription_checkout(
                self.user_id,
                str(uuid.uuid4()),
                self.email,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(reserve_once, range(workers)))

        self.assertEqual(sum(1 for item in results if item["created"]), 1)
        self.assertTrue(
            all(
                item["created"] or item["reason"] == "checkout_in_progress"
                for item in results
            )
        )

    def test_watchlist_replace_is_atomic_and_removes_stale_snapshots(self):
        first = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
        second = ["NVDA", "TSLA", "AMD", "INTC", "ORCL"]
        db.set_watch_symbols(self.user_id, first, max_symbols=5)
        db.upsert_signal_snapshot(self.user_id, "AAPL", {"overall": "bullish"})
        db.add_signal_event(self.user_id, "AAPL", {"current_label": "买入观察"})
        barrier = threading.Barrier(2)

        def replace(symbols):
            barrier.wait(timeout=5)
            return db.set_watch_symbols(self.user_id, symbols, max_symbols=5)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(replace, (first, second)))

        saved = db.get_watch_symbols(self.user_id)
        self.assertEqual(len(saved), 5)
        self.assertIn(set(saved), (set(first), set(second)))
        self.assertTrue(set(db.get_signal_snapshots(self.user_id)).issubset(set(saved)))
        self.assertTrue(
            {event["symbol"] for event in db.list_signal_events(self.user_id)}.issubset(set(saved))
        )

    def test_usage_counter_is_atomic_under_concurrency(self):
        workers = 16
        barrier = threading.Barrier(workers)
        metric = f"concurrency_{uuid.uuid4().hex[:12]}"

        def consume_once():
            barrier.wait(timeout=5)
            return db.consume_usage(
                self.user_id,
                metric,
                "2026-08-07",
                5,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda _index: consume_once(), range(workers)))

        self.assertEqual(sum(1 for allowed, _used in results if allowed), 5)
        self.assertEqual(max(used for _allowed, used in results), 5)


if __name__ == "__main__":
    unittest.main()
