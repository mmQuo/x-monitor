import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import clean_worker
import daily_worker
import event_worker
from clean_store import CleanStore
from event_store import EventStore, ProcessingPending
from model_tasks import PROMPT_BYTES, WorkflowError
from report_store import ReportStore

NOW = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)


class Model:
    def __init__(self):
        self.requests = []
        self.extracted = []

    def post(self, url, **kwargs):
        body = kwargs["json"]
        assert sum(len(m["content"].encode()) for m in body["messages"]) <= PROMPT_BYTES
        payload = json.loads(body["messages"][1]["content"])
        self.requests.append(payload)
        if "posts" in payload:
            self.extracted += [p["text"] for p in payload["posts"]]
            result = {"events": [], "ignored_ids": []}
            for post in payload["posts"]:
                if not post["text"].strip():
                    result["ignored_ids"].append(post["id"])
                    continue
                subject = post["text"].split()[0]
                result["events"].append({"title": subject + " event", "summary": post["text"][:180],
                    "category": "AI资讯", "importance": 4, "reason": "Service changed",
                    "status": "报道", "source_ids": [post["id"]], "entities": [subject], "quote": post["text"][:80]})
        elif "facts" in payload:
            groups = {}
            for fact in payload["facts"]:
                subject = fact["entities"][0]
                if subject not in groups:
                    matches = [e["id"] for e in payload["catalog"] if subject in e["entities"]]
                    groups[subject] = {"event_ids": matches, "fact_ids": []}
                groups[subject]["fact_ids"].append(fact["id"])
            result = {"groups": list(groups.values())}
        elif "sources" in payload:
            candidates = payload.get("events", [])
            item = dict(candidates[-1] if candidates else payload["candidate"])
            item["source_ids"] = [s["id"] for s in payload["sources"]][-3:]
            item["timeline"] = [{"text": item["summary"], "source_ids": [item["source_ids"][-1]]}]
            result = {"event": item, "changed": True}
        else:
            # One event in these report fixtures; selection preserves its evidence.
            result = {"events": [payload["events"][-1]]}
        return event_worker.httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "config").mkdir()
        (self.base / "config/targets.txt").write_text("tibo\n")
        self.store = EventStore(self.base)
        self.model = Model()
        self.processor = event_worker.EventProcessor(self.store, self.model, "v1")
        self.posts = []

    def add(self, id="1", text="Alpha service launched.", stamp=NOW):
        post = {"id": id, "username": "tibo", "created_at": stamp.isoformat(), "text": text, "has_media": False}
        self.posts = [p for p in self.posts if p["id"] != id] + [post]
        (self.base / "tibo_posts.jsonl").write_text("".join(json.dumps(p) + "\n" for p in self.posts))
        return post

    def process(self, now=NOW):
        clean_worker.process_once(self.base, now=now)
        result = self.processor.process_once(self.base, now=now)
        self.assertEqual(result["errors"], 0, self.store.meta("error"))
        return result

    def report(self, store):
        run_id = store.enqueue("2026-01-02")
        flow = daily_worker.Workflow(store, run_id, self.model)
        with patch.object(CleanStore, "prepare", side_effect=AssertionError("report cleaned posts")), \
             patch.object(flow, "extract_group", side_effect=AssertionError("report extracted facts")):
            flow.run(store.claim(), self.base)
        return store.get(run_id)

    def test_background_works_without_reports_and_restart_reuses_facts(self):
        self.add()
        self.process()
        self.assertFalse((self.base / "report_data").exists())
        self.assertEqual(self.store.status(NOW)["ready"], 1)
        self.assertEqual(len(self.store.top(NOW)), 1)
        calls = len(self.model.requests)
        self.processor = event_worker.EventProcessor(EventStore(self.base), self.model, "v1")
        self.process()
        self.assertEqual(len(self.model.requests), calls)

    def test_morning_and_afternoon_reports_only_extract_new_posts_once(self):
        self.add()
        self.process()
        reports = ReportStore(self.base / "report_data")
        morning = self.report(reports)
        self.assertEqual(morning["result"]["post_count"], 1)
        frozen = json.dumps(morning["snapshot"], sort_keys=True)
        afternoon_time = NOW + timedelta(hours=3)
        self.add("2", "Alpha service restored after outage.", afternoon_time)
        self.process(afternoon_time)
        afternoon = self.report(reports)
        self.assertEqual(afternoon["result"]["post_count"], 2)
        self.assertEqual(self.model.extracted, ["Alpha service launched.", "Alpha service restored after outage."])
        self.assertEqual(json.dumps(reports.get(morning["id"])["snapshot"], sort_keys=True), frozen)
        self.assertNotIn("restored", frozen)
        self.assertEqual(len(self.store.top(afternoon_time)), 1)
        before = len(self.model.requests)
        daily_worker.Workflow(reports, morning["id"], self.model).run(morning, self.base)
        self.assertEqual(len(self.model.requests), before)

    def test_content_and_extraction_version_invalidate_only_relevant_inputs(self):
        self.add()
        self.add("2", "Beta model released.")
        self.process()
        original = len(self.model.extracted)
        self.add(text="Alpha service launch was cancelled.")
        clean_worker.process_once(self.base, now=NOW)
        event_worker.sync_cleaned(self.base, self.store, "v1", NOW)
        self.assertEqual([e["title"] for e in self.store.top(NOW)], ["Beta event"])
        self.process()
        self.assertEqual(self.model.extracted[original:], ["Alpha service launch was cancelled."])
        self.assertEqual(len(self.store.top(NOW)), 2)
        self.processor = event_worker.EventProcessor(self.store, self.model, "v2")
        self.process()
        self.assertEqual(len(self.model.extracted), original + 3)
        self.assertEqual(len(self.store.top(NOW)), 2)

    def test_pending_snapshot_is_read_only_and_never_triggers_work(self):
        self.add()
        with patch.object(CleanStore, "prepare", side_effect=AssertionError("unexpected cleaning")):
            snapshot = daily_worker.snapshot_posts("2026-01-02", self.base)
            with self.assertRaises(ProcessingPending):
                EventStore(self.base, readonly=True).attach(snapshot)
        self.assertEqual(self.model.requests, [])
        self.assertFalse((self.base / "clean_data").exists())
        clean_worker.process_once(self.base, now=NOW)
        with self.assertRaises(ProcessingPending):
            self.store.attach(daily_worker.snapshot_posts("2026-01-02", self.base))
        self.process()
        self.assertEqual(len(self.store.attach(daily_worker.snapshot_posts("2026-01-02", self.base))["facts"]), 1)
        with EventStore(self.base, readonly=True).connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO meta VALUES ('reader-write','true')")

    def test_repeated_copies_do_not_refresh_age_and_expiration_needs_no_model(self):
        self.add()
        self.process()
        progress = self.store.top(NOW)[0]["progress_at"]
        later = NOW + timedelta(hours=8)
        self.add("2", stamp=later)
        self.process(later)
        self.assertEqual(self.store.top(later)[0]["progress_at"], progress)
        self.assertEqual(self.store.top(later)[0]["importance"], 4)
        calls = len(self.model.requests)
        self.assertEqual(self.store.top(NOW + timedelta(hours=25)), [])
        self.assertEqual(len(self.model.requests), calls)

    def test_missing_and_cross_post_citations_and_invented_quotes_rejected(self):
        record = {"1": {"text": "Alpha service launched."}, "2": {"text": "Beta service ended."}}
        item = {"title": "Alpha", "summary": "Launch", "category": "AI资讯", "importance": 4,
                "reason": "Launch", "status": "报道", "source_ids": ["1", "2"], "entities": ["Alpha"], "quote": "Alpha"}
        with self.assertRaises(WorkflowError):
            event_worker.fact_schema({"events": [item], "ignored_ids": []}, record)
        item["source_ids"] = ["1"]
        item["quote"] = "Beta service ended."
        with self.assertRaises(WorkflowError):
            event_worker.fact_schema({"events": [item], "ignored_ids": ["2"]}, record)
        item["quote"] = "Alpha"
        with self.assertRaises(WorkflowError):
            event_worker.fact_schema({"events": [item], "ignored_ids": []}, record)
        for groups in ([], [{"fact_ids": ["F1", "F1"], "event_ids": []}], [{"fact_ids": ["F1"], "event_ids": ["E99"]}]):
            with self.assertRaises(WorkflowError):
                event_worker.route_schema({"groups": groups}, {"F1": "one"}, {"E1": "event"})

    def test_old_posts_are_not_backfilled_and_future_posts_are_excluded(self):
        old = self.add("1", stamp=NOW - timedelta(days=10))
        self.add("2", stamp=NOW + timedelta(days=1))
        CleanStore(self.base).prepare([old])
        self.process()
        self.assertEqual(self.model.extracted, [])

    def test_bad_record_isolated_and_successful_records_survive_restart(self):
        self.add("1", "Alpha valid update.")
        self.add("2", "Beta valid update.")
        clean_worker.process_once(self.base, now=NOW)
        event_worker.sync_cleaned(self.base, self.store, "v1", NOW)
        rows = self.store.pending_records(event_worker.cutoff_at(NOW))
        original = self.processor.tasks.call
        def call(key, instruction, payload, validate):
            if any(p["text"].startswith("Beta") for p in payload.get("posts", [])):
                raise WorkflowError("Bad excerpt")
            return original(key, instruction, payload, validate)
        self.processor.tasks.call = call
        with self.assertRaises(WorkflowError):
            self.processor.extract(rows)
        remaining = self.store.pending_records(event_worker.cutoff_at(NOW))
        self.assertEqual([r["record"]["text"] for r in remaining], ["Beta valid update."])
        self.processor = event_worker.EventProcessor(self.store, self.model, "v1")
        self.process()
        self.assertEqual(self.model.extracted.count("Alpha valid update."), 1)
        self.assertEqual(len(self.store.top(NOW)), 2)

    def test_long_source_updates_remain_bounded_and_keep_all_original_fragments(self):
        raw = "Alpha " + "Long update with exact details. " * 180
        self.add(text=raw)
        self.process()
        frozen = self.store.attach(daily_worker.snapshot_posts("2026-01-02", self.base))
        evidence = sorted(frozen["evidence"], key=lambda r: r["part"])
        self.assertEqual("".join(r["text"] for r in evidence), raw)
        self.assertGreater(len(evidence), 1)
        self.assertEqual({r["id"] for r in frozen["records"]}, {r["id"] for r in evidence})

    def test_reports_filter_event_facts_by_day_instead_of_using_later_summary(self):
        self.add("1", "Alpha preliminary statement.", NOW - timedelta(hours=20))
        self.add("2", "Alpha a later correction.", NOW)
        self.process()
        snapshot = self.store.attach(daily_worker.snapshot_posts("2026-01-01", self.base))
        self.assertEqual(len(snapshot["facts"]), 1)
        self.assertIn("preliminary", snapshot["facts"][0]["summary"])
        self.assertNotIn("correction", json.dumps(snapshot))

    def test_waiting_report_yields_queue_and_can_resume(self):
        reports = ReportStore(self.base / "reports")
        run_id = reports.enqueue("2026-01-02")
        job = reports.claim()
        with patch.object(daily_worker.Workflow, "run", side_effect=ProcessingPending(2, 3)):
            daily_worker.process_job(reports, job)
        state = reports.get(run_id)
        self.assertEqual((state["status"], state["done"], state["total"]), ("queued", 2, 3))
        self.assertIsNone(reports.claim())
        with reports.connect() as db:
            db.execute("UPDATE runs SET retry_at='' WHERE id=?", (run_id,))
        self.assertEqual(reports.claim()["id"], run_id)

    def test_disable_source_removes_invalid_live_evidence(self):
        self.add()
        self.process()
        (self.base / "config/targets.txt").write_text("")
        self.process()
        self.assertEqual(self.store.top(NOW), [])

    def test_web_auth_escaping_and_stream(self):
        self.add(text='<script>alert("x")</script> event changed.')
        self.process()
        os.environ.setdefault("XMONITOR_WEB_SECRET", "test-only")
        os.environ.setdefault("XMONITOR_WEB_USERNAME", "test")
        os.environ.setdefault("XMONITOR_WEB_PASSWORD_HASH", "test-only")
        app = importlib.import_module("web_app").app
        with patch("event_web.EventStore", return_value=self.store), patch("event_web.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            client = app.test_client()
            for path in ("/top", "/top/status", "/top/stream"):
                self.assertEqual(client.get(path).status_code, 302)
            with client.session_transaction() as session:
                session["authenticated"] = True
            page = client.get("/top")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(b'<script>alert', page.data)
            self.assertIn(b'&lt;script&gt;', page.data)
            self.assertEqual(client.get("/top/status").status_code, 200)
            stream = client.get("/top/stream", buffered=False)
            self.assertEqual(stream.headers["X-Accel-Buffering"], "no")
            self.assertEqual(next(stream.response), b"retry: 5000\n\n")
            self.assertIn(b"event: update", next(stream.response))
            stream.close()


if __name__ == "__main__":
    unittest.main()
