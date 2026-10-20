"""Offline tests: no requests to real websites are made."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scraper import MonitorError, check_robots, load_config, load_state, parse_listings, run

TEST_CONFIG = {
    "website": {
        "listings_url": "https://listings.example.org/offers",
        "permission_confirmed": True,
        "permission_basis": "written-permission",
        "permission_reference": "Written authorization retained by operator",
    },
    "selectors": {
        "item": ".card", "title": ".title", "link": "a",
        "price": ".price", "location": ".region"
    },
    "filter_keywords": ["Leiden"],
    "minimum_interval_seconds": 3600,
}
HTML = '''<div class="card"><a href="/room/1">Details</a><h2 class="title">Student studio</h2>
    <span class="price">€500</span><span class="region">Leiden</span></div>
<div class="card"><a href="/room/2">Details</a><h2 class="title">Student studio</h2>
    <span class="region">Amsterdam</span></div>
<div class="card"><a href="https://unrelated.example/other">Details</a><h2 class="title">Studio</h2>
    <span class="region">Leiden</span></div>'''


class ScraperTests(unittest.TestCase):
    def test_parser_restricts_origin_and_filters(self):
        results = parse_listings(HTML, TEST_CONFIG)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://listings.example.org/room/1")
        self.assertEqual(results[0]["price"], "€500")

    def test_requires_explicit_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = json.loads(json.dumps(TEST_CONFIG))
            cfg["website"]["permission_confirmed"] = False
            p = Path(folder) / "config.json"
            p.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(MonitorError, "Permission not confirmed"):
                load_config(p)

    def test_fail_closed_when_robots_disallows(self):
        with tempfile.TemporaryDirectory() as folder:
            config_file = Path(folder) / "config.json"
            state_file = Path(folder) / "seen.json"
            config_file.write_text(json.dumps(TEST_CONFIG))
            with patch("scraper.check_robots", side_effect=MonitorError("robots denied")):
                with self.assertRaisesRegex(MonitorError, "robots denied"):
                    run(config_file, state_file, dry_run=True)
            self.assertFalse(state_file.exists())

    def test_dry_run_records_rate_limit_but_not_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            config_file = Path(folder) / "config.json"
            state_file = Path(folder) / "seen.json"
            config_file.write_text(json.dumps(TEST_CONFIG))
            with patch("scraper.check_robots"), patch("scraper.fetch_listings_html", return_value=HTML):
                self.assertEqual(run(config_file, state_file, dry_run=True), 0)
            self.assertTrue(state_file.exists())
            self.assertEqual(json.loads(state_file.read_text())["seen_ids"], [])
            with patch("scraper.check_robots") as check, patch("scraper.fetch_listings_html") as fetch:
                self.assertEqual(run(config_file, state_file, dry_run=True), 0)
                check.assert_not_called()
                fetch.assert_not_called()

    def test_robots_denies_disallowed_paths(self):
        with patch("scraper._limited_get", return_value=(200, "User-agent: *\nDisallow: /offers", "text/plain")):
            with self.assertRaisesRegex(MonitorError, "robots.txt disallows"):
                check_robots(object(), TEST_CONFIG["website"]["listings_url"])

    def test_failed_alert_is_not_marked_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            config_file = Path(folder) / "config.json"
            state_file = Path(folder) / "seen.json"
            config_file.write_text(json.dumps(TEST_CONFIG))
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "123"}), \
                 patch("scraper.check_robots"), \
                 patch("scraper.fetch_listings_html", return_value=HTML), \
                 patch("scraper.telegram_alert", side_effect=MonitorError("send failed")):
                with self.assertRaisesRegex(MonitorError, "send failed"):
                    run(config_file, state_file)
            self.assertEqual(json.loads(state_file.read_text())["seen_ids"], [])

    def test_legacy_seen_ids_not_reused_across_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "seen.json"
            p.write_text('["some-old-studio-id"]')
            state = load_state(p, "my-other-source-hash")
            self.assertEqual(state["seen_ids"], [])


if __name__ == "__main__":
    unittest.main()
