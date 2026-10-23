"""Exercise macro filters using isolated post files, without model or X calls."""

import csv
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("XMONITOR_WEB_SECRET", "macro-filter-test-secret")
os.environ.setdefault("XMONITOR_WEB_USERNAME", "test-user")
os.environ.setdefault("XMONITOR_WEB_PASSWORD_HASH", "unused-in-authenticated-tests")

import ai_worker
import daily_worker
import web_app


class MacroTopicIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        (self.base / "config").mkdir()
        self.stamp = datetime.now(timezone.utc).isoformat()
        self.fixtures = [
            ("1001", "BLS_gov", "通胀与利率", "Fixture CPI actual 3.0%, expected 3.1%"),
            ("1002", "ReutersWorld", "地缘冲突", "Fixture ceasefire negotiations"),
            ("1003", "EIAgov", "能源与石油", "Fixture oil inventory report"),
            ("1004", "Aster_DEX", "链上资金", "Fixture on-chain gas fees"),
            ("1005", "BLS_gov", None, "Fixture recruitment notice awaiting enrichment"),
            ("1006", "federalreserve", "通胀与利率", "Fixture policy rate statement"),
            ("1007", "business", "市场与宏观", "Fixture company earnings with legacy category"),
            ("1008", "OPECSecretariat", "能源与石油", "Fixture production decision"),
        ]
        names = list(dict.fromkeys(row[1] for row in self.fixtures))
        self.targets = self.base / "config" / "targets.txt"
        self.targets.write_text("# Test accounts\n" + "\n".join(names) + "\n")
        annotations = {}
        for post_id, account, category, text in self.fixtures:
            post = {"id": post_id, "username": account, "created_at": self.stamp,
                    "observed_at": self.stamp, "text": text,
                    "url": f"https://x.com/{account}/status/{post_id}"}
            with (self.base / f"{account.lower()}_posts.jsonl").open("a") as output:
                output.write(json.dumps(post) + "\n")
            if category:
                annotations[post_id] = {"category": category, "summary": text,
                                        "translation": "测试译文：" + text}
        annotation_file = self.base / "annotations.json"
        annotation_file.write_text(json.dumps(annotations))
        self.addCleanup(patch.stopall)
        patch.multiple(web_app, BASE=self.base, TARGETS=self.targets,
                       ANNOTATIONS=annotation_file, STATUS=self.base / "status.json",
                       LINK_CACHE=self.base / "links.json").start()
        patch.dict(web_app.app.config, TESTING=True, SESSION_COOKIE_SECURE=False).start()
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def filtered_ids(self, **query):
        response = self.client.get("/", query_string={**query, "_partial": "1"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        ids = set(re.findall(r'data-post-id="(\d+)"', data["rows_html"]))
        self.assertEqual(data["count"], len(ids))
        return ids

    def test_parent_filter_contains_only_the_three_macro_topics(self):
        self.assertEqual(self.filtered_ids(category="宏观与地缘"),
                         {"1001", "1002", "1003", "1006", "1008"})
        self.assertEqual(len(self.filtered_ids()), 8)
        self.assertEqual(self.filtered_ids(category="市场与宏观"), {"1007"})
        self.assertEqual(self.filtered_ids(category="链上资金"), {"1004"})

    def test_leaf_filters_and_selected_options_round_trip(self):
        for category, expected in [("通胀与利率", {"1001", "1006"}),
                                   ("地缘冲突", {"1002"}),
                                   ("能源与石油", {"1003", "1008"})]:
            with self.subTest(category=category):
                self.assertEqual(self.filtered_ids(category=category), expected)
                html = self.client.get("/", query_string={"category": category}).get_data(as_text=True)
                self.assertIn(f'<option value="{category}" selected>', html)
                self.assertEqual(set(re.findall(r'data-post-id="(\d+)"', html)), expected)

    def test_parent_filter_composes_with_account_and_search(self):
        self.assertEqual(self.filtered_ids(category="宏观与地缘", account="bls_GOV"), {"1001"})
        self.assertEqual(self.filtered_ids(category="宏观与地缘", q="OIL"), {"1003"})
        self.assertEqual(self.filtered_ids(category="宏观与地缘", q="recruitment"), set())
        self.assertEqual(len(self.filtered_ids(category="not-a-category")), 8)

    def test_model_categories_survive_validation_and_csv_export(self):
        for category in ["通胀与利率", "地缘冲突", "能源与石油", "市场与宏观", "财经新闻", "公司与产品"]:
            parsed = ai_worker.parse_result(json.dumps({"summary": "测试摘要", "category": category,
                                                       "translation": "测试译文"}))
            self.assertEqual(parsed["category"], category)
        response = self.client.get("/targets/EIAgov/export", query_string={"format": "csv"})
        self.assertEqual(response.status_code, 200)
        exported = list(csv.DictReader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
        self.assertEqual(exported[0]["分类"], "能源与石油")

    def test_daily_report_snapshot_includes_all_five_new_sources(self):
        day = datetime.fromisoformat(self.stamp).astimezone(daily_worker.TZ).date().isoformat()
        snapshot = daily_worker.snapshot_posts(day, self.base)
        expected = {"ReutersWorld", "BLS_gov", "federalreserve", "EIAgov", "OPECSecretariat"}
        self.assertTrue(expected.issubset({post["username"] for post in snapshot["posts"]}))
        self.assertEqual(len(snapshot["posts"]), len(self.fixtures))
        fragments, _ = daily_worker.prepare_posts(snapshot)
        self.assertTrue({"@" + name for name in expected}.issubset({post["account"] for post in fragments}))


if __name__ == "__main__":
    unittest.main()
