from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.policy import default as default_policy
from unittest import mock

import auth
import brief_scheduler
import notifications
import security
import secret_store
import server


class ReportSafetyTests(unittest.TestCase):
    def test_key_levels_stay_on_the_correct_side_of_price(self):
        support, resistance = server.classify_key_levels(
            100.0,
            [105, 98, 98.01, 90],
            [95, 102, 110],
        )
        self.assertTrue(all(value < 100 for value in support))
        self.assertTrue(all(value > 100 for value in resistance))
        self.assertEqual(support, [98.0, 90.0])
        self.assertEqual(resistance, [102.0, 110.0])

    def test_brief_preferences_validate_time_and_channels(self):
        preferences = server.normalize_brief_preferences(
            {
                "enabled": True,
                "delivery_time": "18:30",
                "timezone": "Asia/Shanghai",
                "channels": ["email", "telegram", "unknown"],
                "min_confidence": 55,
            }
        )
        self.assertEqual(preferences["channels"], ["email", "telegram"])
        self.assertEqual(preferences["min_confidence"], 55.0)
        with self.assertRaises(ValueError):
            server.normalize_brief_preferences({"enabled": True, "delivery_time": "25:00"})


class SecurityTests(unittest.TestCase):
    def test_production_cookie_is_secure(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            self.assertIn("; Secure", auth.session_cookie_header("token"))

    def test_html_headers_include_csp(self):
        headers = security.response_headers("text/html; charset=utf-8")
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_localhost_and_loopback_are_same_local_origin(self):
        handler = mock.Mock()
        handler.headers = {"Origin": "http://localhost:8787", "Host": "127.0.0.1:8787"}
        self.assertTrue(security.request_origin_allowed(handler))

    def test_cross_site_origin_is_rejected(self):
        handler = mock.Mock()
        handler.headers = {"Origin": "https://evil.example", "Host": "127.0.0.1:8787"}
        self.assertFalse(security.request_origin_allowed(handler))

    def test_null_origin_is_allowed_only_for_loopback_peer_and_host(self):
        handler = mock.Mock()
        handler.headers = {"Origin": "null", "Host": "127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 54321)
        self.assertTrue(security.request_origin_allowed(handler))
        handler.client_address = ("192.0.2.10", 54321)
        self.assertFalse(security.request_origin_allowed(handler))

    def test_local_development_loopback_tolerates_browser_origin_rewriting(self):
        handler = mock.Mock()
        handler.headers = {"Origin": "chrome-extension://rewritten", "Host": "127.0.0.1:8787"}
        handler.client_address = ("127.0.0.1", 54321)
        with mock.patch.dict(os.environ, {"APP_ENV": "development"}, clear=False):
            self.assertTrue(security.request_origin_allowed(handler))
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            self.assertFalse(security.request_origin_allowed(handler))


class NotificationTests(unittest.TestCase):
    def test_wechat_payload_uses_markdown(self):
        with mock.patch.object(notifications, "_post_json") as post:
            notifications.send(
                "wechat", "user@example.com", "Brief", "Body",
                config={"webhook_url": "https://example.test/hook"},
            )
        url, payload = post.call_args.args
        self.assertEqual(url, "https://example.test/hook")
        self.assertEqual(payload["msgtype"], "markdown")

    def test_transient_failure_is_retried(self):
        with (
            mock.patch.object(
                notifications,
                "send",
                side_effect=[notifications.NotificationError("temporary"), None],
            ) as send,
            mock.patch.object(notifications.time, "sleep"),
        ):
            attempts = notifications.send_with_retry("email", "a@example.com", "Brief", "Body")
        self.assertEqual(attempts, 2)
        self.assertEqual(send.call_count, 2)

    def test_nbsp_at_position_24_survives_ascii_smtp_encoding(self):
        # smtplib.SMTP.sendmail/data does `payload.encode("ascii")` when the
        # message is still a str. NBSP at index 24 is the production crash:
        # UnicodeEncodeError: 'ascii' codec can't encode character '\xa0'
        # in position 24.
        ascii_with_nbsp = ("P" * 24) + "\u00a0" + "USD"
        with self.assertRaises(UnicodeEncodeError) as raised:
            ascii_with_nbsp.encode("ascii")
        self.assertEqual(raised.exception.start, 24)
        self.assertEqual(raised.exception.object[24], "\u00a0")
        normalized_ascii = notifications.normalize_outbound_text(ascii_with_nbsp)
        normalized_ascii.encode("ascii")
        self.assertEqual(normalized_ascii[24], " ")

        body = ("P" * 24) + "\u00a0" + "美元 简报"
        subject = "MarketBrief AI\u00a0每日简报"
        self.assertNotIn("\u00a0", notifications.normalize_outbound_text(body))
        self.assertNotIn("\u00a0", notifications.normalize_outbound_text(subject))

        message = notifications.build_email_message(
            "MarketBrief AI <noreply@example.com>",
            "user@example.com",
            subject,
            body,
        )
        raw = message.as_bytes(policy=message.policy)
        # The exact encoder that crashed deliveries after 3 retries.
        if isinstance(raw, str):
            encoded = raw.encode("ascii")
        else:
            encoded = raw
            raw.decode("ascii")
        message.as_string().encode("ascii")
        parsed = message_from_bytes(encoded, policy=default_policy)
        rendered = parsed.get_content()
        self.assertIn("美元", rendered)
        self.assertIn("简报", rendered)
        self.assertNotIn("\u00a0", rendered)
        self.assertEqual(parsed["Subject"], "MarketBrief AI 每日简报")

    def test_send_email_transmits_ascii_payload_for_unicode_brief(self):
        captured: dict[str, object] = {}

        class DummySMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                return None

            def sendmail(self, from_addr, to_addrs, msg):
                if isinstance(msg, str):
                    msg = msg.encode("ascii")
                else:
                    msg.decode("ascii")
                captured["from"] = from_addr
                captured["to"] = to_addrs
                captured["msg"] = msg

        env = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "465",
            "SMTP_USER": "noreply@example.com",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM": "MarketBrief AI <noreply@example.com>",
            "SMTP_USE_SSL": "1",
            "SMTP_USE_TLS": "0",
        }
        body = ("P" * 24) + "\u00a0" + "美元"
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(notifications.smtplib, "SMTP_SSL", DummySMTP):
                notifications.send_email("user@example.com", "每日简报\u00a0", body)
        self.assertEqual(captured["from"], "noreply@example.com")
        self.assertEqual(captured["to"], ["user@example.com"])
        self.assertIn(b"utf-8", captured["msg"])
        self.assertNotIn("\u00a0".encode("utf-8"), captured["msg"])

    def test_permanent_failure_reports_retry_count(self):
        with (
            mock.patch.object(notifications, "send", side_effect=RuntimeError("offline")) as send,
            mock.patch.object(notifications.time, "sleep"),
        ):
            with self.assertRaisesRegex(notifications.NotificationError, "重试 3 次"):
                notifications.send_with_retry("email", "a@example.com", "Brief", "Body")
        self.assertEqual(send.call_count, 3)


class SchedulerTests(unittest.TestCase):
    def test_due_job_runs_after_delivery_time_for_restart_catchup(self):
        runner = mock.Mock()
        scheduler = brief_scheduler.BriefScheduler(runner)
        preferences = {
            "user_id": 1,
            "email": "a@example.com",
            "timezone": "Asia/Shanghai",
            "delivery_time": "00:00",
        }
        with mock.patch.object(
            brief_scheduler.db, "list_enabled_brief_preferences", return_value=[preferences]
        ):
            scheduler.run_once()
        runner.assert_called_once()
        self.assertRegex(runner.call_args.args[0]["local_date"], r"^\d{4}-\d{2}-\d{2}$")


def _recent_bars(count: int = 40) -> list[dict[str, str]]:
    today = datetime.now(timezone.utc).date()
    return [
        {"date": (today - timedelta(days=count - 1 - index)).isoformat()}
        for index in range(count)
    ]


class DataHealthTests(unittest.TestCase):
    def test_btc_usd_crypto_is_not_a_mapping_mismatch(self):
        health = server.build_data_health(
            "BTC-USD",
            {
                "exchangeName": "CCC",
                "instrumentType": "CRYPTOCURRENCY",
                "shortName": "Bitcoin USD",
                "longName": "Bitcoin USD",
                "currency": "USD",
            },
            _recent_bars(),
        )
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["status_label"], "数据正常")
        self.assertEqual(health["suggestions"], [])
        self.assertFalse(any("去掉 -USD" in item for item in health["warnings"]))
        self.assertEqual(server.detect_asset_type("BTC-USD"), "crypto")

    def test_aapl_equity_stays_ok(self):
        health = server.build_data_health(
            "AAPL",
            {
                "exchangeName": "NMS",
                "instrumentType": "EQUITY",
                "shortName": "Apple Inc.",
            },
            _recent_bars(),
        )
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["suggestions"], [])

    def test_tokenized_equity_with_usd_suffix_still_warns(self):
        health = server.build_data_health(
            "SPACEX-USD",
            {
                "exchangeName": "CCC",
                "instrumentType": "CRYPTOCURRENCY",
                "shortName": "SpaceX tokenized stock (PreStocks) USD",
            },
            _recent_bars(),
        )
        self.assertEqual(health["status"], "mismatch")
        self.assertEqual(health["status_label"], "疑似代码映射错误")
        self.assertEqual(health["suggestions"][0]["symbol"], "SPACEX")
        self.assertTrue(any("去掉 -USD" in item for item in health["warnings"]))


class SecretStoreTests(unittest.TestCase):
    def test_notification_config_round_trip_is_encrypted(self):
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": "test-key-a"}, clear=False):
            encrypted = secret_store.encrypt_config({"webhook_url": "https://example.test/private"})
            self.assertNotIn(b"example.test", encrypted)
            self.assertEqual(secret_store.decrypt_config(encrypted)["webhook_url"], "https://example.test/private")

    def test_wrong_key_cannot_decrypt_secret(self):
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": "tenant-a"}, clear=False):
            encrypted = secret_store.encrypt_config({"bot_token": "secret"})
        with mock.patch.dict(os.environ, {"NOTIFICATION_ENCRYPTION_KEY": "tenant-b"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "无法解密"):
                secret_store.decrypt_config(encrypted)


if __name__ == "__main__":
    unittest.main()
