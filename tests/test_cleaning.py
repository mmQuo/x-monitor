import json
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import daily_worker
import monitor
import post_cleaner as cleaner
from clean_store import CleanStore
from clean_worker import process_once
from report_store import TZ


class CleaningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "config").mkdir()
        (self.base / "config/targets.txt").write_text("tibo\nthsottiaux\n")
        self.store = CleanStore(self.base)

    def post(self, id="1", text="Model v1.2 costs $8, not $10.", **extra):
        return {"id": id, "username": "tibo", "created_at": "2026-01-02T08:00:00+08:00",
                "text": text, **extra}

    def write(self, posts, mode="w"):
        with (self.base / "tibo_posts.jsonl").open(mode) as file:
            for post in posts:
                file.write(json.dumps(post, ensure_ascii=False) + "\n")

    def test_text_cleaning_preserves_facts_emoji_code_and_original(self):
        raw = '  A &amp; B  v1.2\r\n\r\n\r\n$10 → $8；未发布，3.1% @tibo #AI 👩\u200d💻\x00\n```python\n    x = "a  b"\n```'
        post = self.post(text=raw)
        result = cleaner.clean_post(post)
        self.assertIn("A & B v1.2\n\n$10 → $8；未发布，3.1% @tibo #AI 👩\u200d💻", result["clean_text"])
        self.assertIn('    x = "a  b"', result["clean_text"])
        self.assertNotIn("\x00", result["clean_text"])
        self.assertEqual(post["text"], raw)

    def test_link_placeholders_preserve_destinations_and_functional_query(self):
        short = "https://t.co/Abc"
        target = "https://example.com/model?id=42&ref=api&utm_source=x&gclid=abc#pricing"
        result = cleaner.clean_post(self.post(text=f"Model details {short}."), {short: target})
        self.assertEqual(result["clean_text"], "Model details [链接1].")
        link = result["links"][0]
        self.assertEqual(link["expanded"], target)
        self.assertEqual(link["canonical"], "https://example.com/model?id=42&ref=api#pricing")
        self.assertEqual(cleaner.canonical_url("https://example.com/?q=a%20b&x=1&x=2"), "https://example.com/?q=a%20b&x=1&x=2")
        self.assertEqual(cleaner.trim_url("https://example.com/wiki/Model_(AI))."), "https://example.com/wiki/Model_(AI)")

    def test_flags_are_conservative_and_never_drop_records(self):
        fixtures = [("gm", "greeting"), ("https://example.com/news", "link_only"),
                    ("", "empty_text"), ("同意", "context_missing"),
                    ("Giveaway! Follow and retweet to enter to win.", "promotion")]
        for text, expected in fixtures:
            with self.subTest(text=text):
                result = cleaner.clean_post(self.post(text=text))
                self.assertIn(expected, result["flags"])
                self.assertTrue(result["fragments"])
        for text in ("CPI 3.1%", "降息 25 个基点", "We win the model benchmark.", "The company ended its giveaway."):
            self.assertEqual(cleaner.clean_post(self.post(text=text))["flags"], [])

    def test_duplicate_key_preserves_numbers_negations_and_link_identity(self):
        groups = {cleaner.clean_post(self.post(text=text))["duplicate_group"] for text in
                  ("融资 1 亿美元", "融资 10 亿美元", "未融资 1 亿美元", "计划融资 1 亿美元")}
        self.assertEqual(len(groups), 4)
        a = cleaner.clean_post(self.post(text="A https://t.co/One"), {"https://t.co/One": "https://example.com/a?utm_source=x"})
        b = cleaner.clean_post(self.post(id="2", text="A https://t.co/Two"), {"https://t.co/Two": "https://example.com/a?utm_source=y"})
        self.assertEqual(a["duplicate_group"], b["duplicate_group"])
        c = cleaner.clean_post(self.post(text="A https://example.com/b"))
        self.assertNotEqual(a["duplicate_group"], c["duplicate_group"])

    def test_cache_reuse_and_invalidation_for_text_rules_and_resolved_links(self):
        post = self.post(text="Details https://t.co/Abc")
        first, stats = self.store.prepare([post])
        self.assertEqual(stats, {"cleaned": 1, "cached": 0})
        with patch.object(cleaner, "clean_post", side_effect=AssertionError("cache miss")):
            second, stats = self.store.prepare([post], {"https://t.co/Unrelated": "https://example.com/"})
        self.assertEqual(first, second)
        self.assertEqual(stats, {"cleaned": 0, "cached": 1})
        linked, stats = self.store.prepare([post], {"https://t.co/Abc": "https://example.com/article"})
        self.assertEqual(stats["cleaned"], 1)
        self.assertNotEqual(first["1"]["link_hash"], linked["1"]["link_hash"])
        self.assertEqual(self.store.prepare([dict(post, text="Updated release")])[1]["cleaned"], 1)
        with patch.object(cleaner, "CLEANER_VERSION", "2"):
            self.assertEqual(self.store.prepare([dict(post, text="Updated release")])[1]["cleaned"], 1)
        self.assertEqual(self.store.stats()["posts"], 1)

    def test_fragments_are_bounded_and_keep_full_raw_evidence(self):
        raw = "介绍 &amp; 内容\n" * 1200 + "TAIL new model announcement"
        result = cleaner.clean_post(self.post(text=raw))
        self.assertGreater(len(result["fragments"]), 1)
        self.assertEqual("".join(raw[p["start"]:p["end"]] for p in result["fragments"]), raw)
        self.assertIn("TAIL", result["fragments"][-1]["text"])
        for part in result["fragments"]:
            self.assertLessEqual(len(cleaner.encode(part).encode()), cleaner.PART_BYTES)
            self.assertLessEqual(len(cleaner.encode(raw[part["start"]:part["end"]]).encode()), cleaner.SOURCE_BYTES)
        many = " ".join(f"https://t.co/Link{i}" for i in range(100))
        links = {f"https://t.co/Link{i}": "https://example.com/" + "a" * 2000 for i in range(100)}
        self.assertTrue(all(len(cleaner.encode(p).encode()) <= cleaner.PART_BYTES
                            for p in cleaner.clean_post(self.post(text=many), links)["fragments"]))
        code = '```python\n' + 'value = "two  spaces"\n' * 500 + '```'
        self.assertEqual("".join(p["text"] for p in cleaner.clean_post(self.post(text=code))["fragments"]), code)

    def test_worker_only_backfills_today_and_yesterday_and_skips_unchanged_files(self):
        self.write([self.post("1", created_at="2026-01-01T15:59:59+00:00"),
                    self.post("2", created_at="2026-01-01T16:00:00+00:00"),
                    self.post("3", created_at="2026-01-03T11:00:00+08:00")])
        now = datetime(2026, 1, 3, 12, tzinfo=TZ)
        first = process_once(self.base, self.store, now)
        self.assertEqual(first["cleaned"], 2)
        self.assertEqual(self.store.stats()["posts"], 2)
        self.assertEqual(process_once(self.base, self.store, now)["files"], 0)
        self.write([self.post("4", created_at="2026-01-03T11:30:00+08:00")], "a")
        second = process_once(self.base, self.store, now)
        self.assertEqual((second["cleaned"], second["cached"]), (1, 2))
        self.write([self.post("4", text="Edited model announcement", created_at="2026-01-03T11:30:00+08:00")])
        self.assertEqual(process_once(self.base, self.store, now)["cleaned"], 1)

    def test_worker_waits_for_partial_append_and_refreshes_late_link_resolution(self):
        post = self.post(text="Details https://t.co/Abc")
        path = self.base / "tibo_posts.jsonl"
        encoded = json.dumps(post)
        path.write_text(encoded[:20])
        now = datetime(2026, 1, 3, 12, tzinfo=TZ)
        self.assertEqual(process_once(self.base, self.store, now)["cleaned"], 0)
        with path.open("a") as file:
            file.write(encoded[20:] + "\n")
        self.assertEqual(process_once(self.base, self.store, now)["cleaned"], 1)
        (self.base / "link_data").mkdir()
        (self.base / "link_data/links.json").write_text(json.dumps({"https://t.co/Abc": "https://example.com/launch"}))
        self.assertEqual(process_once(self.base, self.store, now)["cleaned"], 1)

    def test_snapshot_freezes_cleaning_and_uses_original_text_for_verification(self):
        raw = "A &amp; B\n\n\nModel release https://t.co/Abc"
        self.write([self.post(text=raw)])
        snapshot = daily_worker.snapshot_posts("2026-01-02", self.base)
        records, _ = daily_worker.prepare_posts(snapshot)
        evidence, _ = daily_worker.prepare_posts(snapshot, evidence=True)
        self.assertIn("A & B", records[0]["text"])
        self.assertIn("[链接1]", records[0]["text"])
        self.assertEqual(evidence[0]["text"], raw)
        self.assertEqual(records[0]["id"], evidence[0]["id"])
        self.store.prepare([self.post(text="Changed text")])
        with patch.object(cleaner, "CLEANER_VERSION", "2"):
            self.assertEqual(daily_worker.prepare_posts(snapshot)[0], records)

    def test_uncertain_context_keeps_each_source_and_legacy_batches_stay_stable(self):
        posts = [self.post("1", text="I launched a model"), self.post("2", text="I launched a model")]
        self.write(posts)
        snapshot = daily_worker.snapshot_posts("2026-01-02", self.base)
        self.assertEqual(len(daily_worker.prepare_posts(snapshot)[0]), 2)
        legacy = {"posts": posts}
        self.assertEqual(daily_worker.prepare_posts(legacy)[1], 1)
        self.assertEqual(len(snapshot["posts"]), 2)

    def test_same_text_with_images_or_quotes_remains_separate(self):
        self.write([self.post("1", text="Release details", has_media=True),
                    self.post("2", text="Release details", has_media=True),
                    self.post("3", text="Release details", has_media=False, quoted_post_id="42")])
        snapshot = daily_worker.snapshot_posts("2026-01-02", self.base)
        self.assertEqual(len(daily_worker.prepare_posts(snapshot)[0]), 3)
        self.assertEqual(self.store.stats()["posts"], 3)

    def test_confirmed_duplicates_keep_all_original_sources_in_snapshot_and_cache(self):
        self.write([self.post("1", has_media=False), self.post("2", has_media=False)])
        snapshot = daily_worker.snapshot_posts("2026-01-02", self.base)
        records, duplicates = daily_worker.prepare_posts(snapshot)
        self.assertEqual((len(records), duplicates), (1, 1))
        self.assertEqual(len(snapshot["posts"]), 2)
        self.assertEqual(self.store.stats()["posts"], 2)
        self.assertEqual(self.store.stats()["duplicate_groups"], 1)

    def test_collector_saves_attachment_and_context_metadata(self):
        account = SimpleNamespace(id=12, username="tibo")
        post = SimpleNamespace(id=123, date=datetime(2026, 1, 2, 12, tzinfo=TZ), user=account,
                               retweetedTweet=None, rawContent="Model release",
                               url="https://x.com/tibo/status/123", links=[],
                               media=SimpleNamespace(photos=[object()], videos=[], animated=[]),
                               inReplyToTweetId=90, quotedTweet=SimpleNamespace(id=91), isQuoteStatus=True)
        class API:
            async def user_by_login(self, name):
                return account
            async def user_tweets(self, user_id, limit):
                yield post
        output = self.base / "tibo_posts.jsonl"
        count = asyncio.run(monitor.poll(API(), "tibo", 10, output, set()))
        self.assertEqual(count, 1)
        saved = json.loads(output.read_text())
        self.assertEqual((saved["has_media"], saved["reply_to_id"], saved["quoted_post_id"]), (True, "90", "91"))


if __name__ == "__main__":
    unittest.main()
