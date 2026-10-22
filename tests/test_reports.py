import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import daily_worker as worker
from report_store import ReportStore, TZ, schedule_due


def event(ids, title="Example model release"):
    return {"title": title, "summary": "The company announced a model release.",
            "category": "AI资讯", "importance": 4, "reason": "New release details.",
            "status": "报道", "source_ids": ids}


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "config").mkdir()
        (self.base / "config/targets.txt").write_text("tibo\nthsottiaux\n")
        self.store = ReportStore(self.base / "reports")

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, id, timestamp, text="Model release announcement"):
        return {"id": id, "created_at": timestamp, "username": "tibo", "text": text}

    def write_posts(self, posts):
        (self.base / "tibo_posts.jsonl").write_text("\n".join(json.dumps(p) for p in posts) + "\n")

    def test_beijing_midnight_and_active_scope(self):
        self.write_posts([self.post(1, "2026-01-01T15:59:59+00:00"),
                          self.post(2, "2026-01-01T16:00:00+00:00"),
                          self.post(3, "2026-01-02T15:59:59+00:00"),
                          self.post(4, "2026-01-02T16:00:00+00:00")])
        (self.base / "removed_posts.jsonl").write_text(json.dumps(self.post(5, "2026-01-02T12:00:00+00:00")))
        snapshot = worker.snapshot_posts("2026-01-02", self.base)
        self.assertEqual([p["id"] for p in snapshot["posts"]], ["2", "3"])
        self.assertEqual(snapshot["posts"][0]["url"], "https://x.com/tibo/status/2")

    def test_dedup_and_long_posts_have_complete_text_coverage(self):
        text = "人工智能\n" * 4000
        snapshot = {"posts": [dict(self.post("1", "2026-01-02T08:00:00+08:00", text)),
                               dict(self.post("2", "2026-01-02T09:00:00+08:00", text))]}
        records, duplicates = worker.prepare_posts(snapshot)
        self.assertEqual(duplicates, 1)
        self.assertEqual("".join(r["text"] for r in records), text)
        self.assertGreater(len(records), 1)
        for batch in worker.batches(records):
            self.assertLessEqual(worker.byte_len(batch), worker.PAYLOAD_BYTES)

    def test_queue_idempotence_resume_and_old_edition_preserved(self):
        run_id = self.store.enqueue("2026-01-02")
        self.assertEqual(self.store.enqueue("2026-01-02"), run_id)
        self.assertEqual(self.store.claim()["id"], run_id)
        self.store.save_step(run_id, "extract:0", {"saved": True})
        self.store.fail(run_id, "temporary network error")
        self.assertEqual(self.store.enqueue("2026-01-02"), run_id)
        self.assertEqual(self.store.step(run_id, "extract:0"), {"saved": True})
        self.store.claim()
        self.store.recover()
        self.assertEqual(self.store.get(run_id)["status"], "queued")
        self.store.complete(run_id, {"events": []})
        new = self.store.enqueue("2026-01-02")
        self.assertNotEqual(new, run_id)
        self.assertEqual(self.store.latest("2026-01-02", completed=True)["id"], run_id)

    def test_schedule_08_beijing_once_and_completed_manual(self):
        self.assertIsNone(schedule_due(self.store, datetime(2026, 1, 3, 7, 59, tzinfo=TZ)))
        run_id = schedule_due(self.store, datetime(2026, 1, 3, 8, 0, tzinfo=TZ))
        self.assertEqual(self.store.get(run_id)["day"], "2026-01-02")
        self.assertIsNone(schedule_due(self.store, datetime(2026, 1, 3, 9, 0, tzinfo=TZ)))
        manual = self.store.enqueue("2026-01-03")
        self.store.snapshot(manual, {"captured_at": "2026-01-04T01:00:00+08:00"}, "test")
        self.store.complete(manual, {"events": []})
        self.assertIsNone(schedule_due(self.store, datetime(2026, 1, 4, 8, 0, tzinfo=TZ)))

    def test_schedule_regenerates_a_partial_day(self):
        manual = self.store.enqueue("2026-01-02")
        self.store.snapshot(manual, {"captured_at": "2026-01-02T17:00:00+08:00"}, "test")
        self.store.complete(manual, {"events": []})
        automatic = schedule_due(self.store, datetime(2026, 1, 3, 8, 0, tzinfo=TZ))
        self.assertNotEqual(automatic, manual)
        self.assertEqual(self.store.get(automatic)["status"], "queued")

    def test_full_queue_does_not_stop_scheduler(self):
        for day in range(1, 15):
            self.store.enqueue(f"2025-12-{day:02}")
        self.assertIsNone(schedule_due(self.store, datetime(2026, 1, 3, 8, 0, tzinfo=TZ)))
        job = self.store.claim()
        self.store.complete(job["id"], {"events": []})
        self.assertIsNotNone(schedule_due(self.store, datetime(2026, 1, 3, 8, 1, tzinfo=TZ)))

    def test_invented_citations_and_missing_inputs_rejected(self):
        with self.assertRaises(worker.WorkflowError):
            worker.extract_schema({"events": [event(["99"])]}, {"1"})
        with self.assertRaises(worker.WorkflowError):
            worker.extract_schema({"events": [event(["1"])]}, {"1", "2"})
        validated = worker.extract_schema({"events": [event(["1"])], "ignored_ids": ["2"]}, {"1", "2"})
        self.assertEqual(len(validated["events"]), 1)

    def test_missing_posts_are_repaired_without_repeating_completed_inputs(self):
        run_id = self.store.enqueue("2026-01-02")
        flow = worker.Workflow(self.store, run_id, None)
        seen = []
        def call(key, instruction, payload, validate):
            ids = [p["id"] for p in payload["posts"]]
            seen.append(ids)
            return validate({"events": [event([ids[0]])], "ignored_ids": []})
        flow.call = call
        result = flow.extract_group("extract:0", "2026-01-02", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual({i for e in result["events"] for i in e["source_ids"]}, {"1", "2", "3"})
        self.assertEqual(seen, [["1", "2", "3"], ["2"], ["3"]])

    def test_output_limit_splits_and_remembers_the_smaller_batches(self):
        run_id = self.store.enqueue("2026-01-02")
        flow = worker.Workflow(self.store, run_id, None)
        seen = []
        def call(key, instruction, payload, validate):
            ids = [p["id"] for p in payload["posts"]]
            seen.append(ids)
            if len(ids) > 1:
                raise worker.OutputLimitError("too long")
            return validate({"events": [event(ids)], "ignored_ids": []})
        flow.call = call
        group = [{"id": "1"}, {"id": "2"}]
        self.assertEqual(len(flow.extract_group("extract:0", "2026-01-02", group)["events"]), 2)
        flow.extract_group("extract:0", "2026-01-02", group)
        self.assertEqual(seen, [["1", "2"], ["1"], ["2"], ["1"], ["2"]])

    def test_long_post_tail_can_be_selected_and_linked_back_to_original(self):
        self.write_posts([self.post(1, "2026-01-02T01:00:00+00:00", "intro " * 1000 + "TAIL launch announced")])
        run_id = self.store.enqueue("2026-01-02")
        job = self.store.claim()
        flow = worker.Workflow(self.store, run_id, None)
        def call(key, instruction, payload, validate):
            if "posts" in payload:
                tail = [p for p in payload["posts"] if "TAIL" in p["text"]]
                other = [p["id"] for p in payload["posts"] if "TAIL" not in p["text"]]
                return validate({"events": [event([p["id"]]) for p in tail], "ignored_ids": other})
            if "events" in payload:
                return validate({"events": payload["events"]})
            self.assertIn("TAIL", payload["sources"][0]["text"])
            item = dict(payload["candidate"])
            item["timeline"] = [{"text": "Launch announced", "source_ids": item["source_ids"]}]
            return validate({"event": item})
        flow.call = call
        flow.run(job, self.base)
        result = self.store.get(run_id)["result"]["events"][0]
        self.assertEqual(result["source_ids"], ["1"])
        self.assertEqual(result["timeline"][0]["source_ids"], ["1"])

    def test_workflow_resume_uses_snapshot_and_checks_raw_evidence(self):
        self.write_posts([self.post(1, "2026-01-02T01:00:00+00:00"),
                          self.post(2, "2026-01-02T02:00:00+00:00", "Launch date confirmed")])
        run_id = self.store.enqueue("2026-01-02")
        job = self.store.claim()
        requests = []
        def fake_post(url, **kwargs):
            body = kwargs["json"]
            self.assertLessEqual(sum(len(m["content"].encode()) for m in body["messages"]), worker.PROMPT_BYTES)
            payload = json.loads(body["messages"][1]["content"])
            requests.append(payload)
            if "posts" in payload:
                result = {"events": [event(["1", "2"])], "ignored_ids": []}
            elif "events" in payload:
                result = {"events": payload["events"]}
            else:
                self.assertEqual(payload["sources"][0]["text"], "Model release announcement")
                item = dict(payload["candidate"])
                item["timeline"] = [{"text": "Launch date confirmed", "source_ids": ["2"]}]
                result = {"event": item}
            class Response:
                is_error = False
                def json(self):
                    return {"usage": {"prompt_tokens": 20, "completion_tokens": 10},
                            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
            return Response()
        class Client:
            post = staticmethod(fake_post)
        flow = worker.Workflow(self.store, run_id, Client())
        flow.run(job, self.base)
        done = self.store.get(run_id)
        self.assertEqual(done["status"], "complete")
        self.assertEqual(done["calls"], 3)
        self.assertEqual(done["result"]["events"][0]["timeline"][0]["published_at"], "2026-01-02T10:00:00+08:00")
        self.write_posts([self.post(3, "2026-01-02T03:00:00+00:00")])
        flow.run(done, self.base)
        self.assertEqual(len(requests), 3)
        self.assertEqual(self.store.get(run_id)["result"]["post_count"], 2)

    def test_empty_day_skips_model_calls(self):
        run_id = self.store.enqueue("2026-01-02")
        worker.Workflow(self.store, run_id, None).run(self.store.claim(), self.base)
        result = self.store.get(run_id)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["result"]["events"], [])

    def test_web_auth_csrf_and_escape_model_output(self):
        os.environ.setdefault("XMONITOR_WEB_SECRET", "test-only")
        os.environ.setdefault("XMONITOR_WEB_USERNAME", "test")
        os.environ.setdefault("XMONITOR_WEB_PASSWORD_HASH", "test-only")
        app = importlib.import_module("web_app").app
        with patch("report_web.ReportStore", return_value=self.store):
            client = app.test_client()
            self.assertEqual(client.get("/reports").status_code, 302)
            self.assertEqual(client.get("/reports/status").status_code, 302)
            self.assertEqual(client.get("/reports/stream").status_code, 302)
            self.assertEqual(client.get("/reports/2026-01-02/export").status_code, 302)
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["csrf"] = "csrf-test"
            self.assertEqual(client.post("/reports/generate", data={"date": "2026-01-02"}).status_code, 400)
            self.assertEqual(client.post("/reports/generate", data={"date": "2026-01-02", "csrf_token": "csrf-test"}).status_code, 302)
            self.assertEqual(client.get("/reports?date=../../secret").status_code, 400)
            run_id = self.store.latest("2026-01-02")["id"]
            source = {"id": "1", "username": "tibo", "created_at": "2026-01-02T12:00:00+08:00",
                      "url": "https://x.com/tibo/status/1", "text": '<script>alert("xss")</script>'}
            item = event(["1"], '<script>alert("title")</script>')
            item["timeline"] = [{"text": "Announcement", "source_ids": ["1"], "published_at": source["created_at"]}]
            self.store.snapshot(run_id, {"posts": [source], "accounts": ["tibo"], "captured_at": "2026-01-03T01:00:00+00:00"}, "test")
            self.store.complete(run_id, {"day": "2026-01-02", "events": [item], "post_count": 1,
                "duplicate_count": 0, "candidate_count": 0, "generated_at": "2026-01-03T01:00:00+00:00"})
            page = client.get("/reports?date=2026-01-02")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b'<script>alert', page.data)
            self.assertIn(b'&lt;script&gt;', page.data)
            self.assertIn("text/markdown", client.get("/reports/2026-01-02/export").content_type)


if __name__ == "__main__":
    unittest.main()
