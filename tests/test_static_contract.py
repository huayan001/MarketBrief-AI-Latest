from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class StaticContractTests(unittest.TestCase):
    def test_trade_review_exposes_collapsible_results_control(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertRegex(
            html,
            r'id="toggleTradeReview"[^>]+aria-controls="tradeReviewBody"[^>]+hidden',
        )

    def test_every_javascript_element_reference_exists(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        referenced = set(re.findall(r'\$\("([^"]+)"\)', app))
        declared = set(re.findall(r'\bid="([^"]+)"', html))
        self.assertEqual(sorted(referenced - declared), [])

    def test_dynamic_and_markup_translation_keys_exist_in_both_locales(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        required = set(re.findall(r'\bt\("([^"]+)"', app))
        required.update(
            re.findall(
                r'data-i18n(?:-placeholder|-aria)?="([^"]+)"',
                html,
            )
        )
        for locale in ("zh-CN.js", "en.js"):
            text = (STATIC / "locales" / locale).read_text(encoding="utf-8")
            available = set(re.findall(r'^\s*"([^"]+)":', text, re.MULTILINE))
            self.assertEqual(
                sorted(required - available),
                [],
                f"missing translation keys in {locale}",
            )

    def test_login_hint_follows_smtp_delivery_keys(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("login.hintSent", app)
        self.assertIn("login.hintLog", app)
        self.assertIn("channels?.email", app)

    def test_trade_review_empty_state_explains_not_connected(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("tradeReview.notConnected", app)
        self.assertIn("tradeReview.configure", app)

    def test_footer_marketing_pages_exist(self):
        for route in ("reports", "learn", "pricing", "about", "methodology", "us-stocks"):
            page = STATIC / route / "index.html"
            self.assertTrue(page.is_file(), route)
            text = page.read_text(encoding="utf-8")
            self.assertIn("<title>", text)


if __name__ == "__main__":
    unittest.main()
