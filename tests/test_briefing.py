from __future__ import annotations

import os
import unittest
from unittest import mock

import auth
import brief_scheduler
import notifications
import security
import secret_store
import server


class ReportSafetyTests(unittest.TestCase):
    def test_email_is_masked_for_authenticated_api_responses(self):
        self.assertEqual(server.mask_email("hyan9677@gmail.com"), "hya***@gmail.com")
        self.assertEqual(server.mask_email("a@example.com"), "a***@example.com")
        self.assertNotIn("secret", server.public_user({"id": 1, "email": "secret@example.com"})["email"])

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

    def test_smtp_app_password_removes_copied_grouping_whitespace(self):
        self.assertEqual(auth.normalize_smtp_password("abcd\u00a0efgh ijkl\tmnop"), "abcdefghijklmnop")


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
