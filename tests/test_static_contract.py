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


if __name__ == "__main__":
    unittest.main()
