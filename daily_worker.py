"""Bounded, resumable daily briefs from locally saved X posts."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ai_worker import API_KEY, BASE_URL, MODEL
from clean_store import read_cleaned
from event_store import EventStore, ProcessingPending
from post_cleaner import CLEANER_VERSION, FLAG_LABELS
from sources import active_source_names, article_url, content_id, source_label, source_path
from report_store import BASE, ReportStore, TZ, day_bounds, encode, now_iso, schedule_due
from report_prompts import EXTRACT, SELECT, VERIFY
from model_tasks import (ModelTasks, WorkflowError, OutputLimitError, CitationError,
    short_references, restore_references, byte_len, clip, source_fragment, batches,
    text_field, citations, event_schema, extract_schema, select_schema)

PROMPT_BYTES = 24000  # UTF-8 bytes conservatively bound token use, independent of tokenizer.
PAYLOAD_BYTES = 15500
SOURCE_BYTES = 3000
MAX_EVENTS = 10
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
STATUSES = {"报道", "待核实", "观点"}


def snapshot_posts(day, base=BASE):
    start, end = day_bounds(day)
    captured_at = now_iso()
    names = active_source_names(base)
    posts = {}
    for name in names:
        path = source_path(base, name)
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as file:
            for line in file:
                try:
                    post = json.loads(line)
                    stamp = datetime.fromisoformat(post["created_at"])
                    if stamp.tzinfo is None or not start <= stamp < end or stamp > datetime.fromisoformat(captured_at):
                        continue
                    post_id = content_id(post["id"])
                    posts[post_id] = {"id": post_id, "username": name,
                        "created_at": stamp.astimezone(TZ).isoformat(),
                        "text": str(post.get("text") or ""),
                        "url": article_url(dict(post, username=name)),
                        "links": post.get("links") or []}
                    for key in ("has_media", "reply_to_id", "quoted_post_id", "source_type", "source_label", "content_kind"):
                        if key in post:
                            posts[post_id][key] = post[key]
                except (ValueError, KeyError, TypeError, OverflowError):
                    continue
    cleaned = read_cleaned(base, posts.values())
    for post in posts.values():
        if post["id"] in cleaned:
            post["cleaned"] = cleaned[post["id"]]
    return {"day": day, "timezone": "Asia/Shanghai", "start": start.isoformat(),
            "end": end.isoformat(), "captured_at": captured_at, "accounts": names,
            "pipeline_version": 2, "cleaner_version": CLEANER_VERSION if len(cleaned) == len(posts) else None,
            "posts": sorted(posts.values(), key=lambda p: (p["created_at"], p["id"]))}


def prepare_clean_posts(snapshot, evidence=False):
    records, seen, duplicates = [], set(), 0
    for post in snapshot["posts"]:
        cleaned = post["cleaned"]
        signature = cleaned["duplicate_group"]
        # Incomplete attachment/context metadata makes equality uncertain. Keep
        # those inputs separate; all records remain in the immutable snapshot.
        can_merge = (signature and post.get("has_media") is False
                     and not post.get("reply_to_id") and not post.get("quoted_post_id")
                     and "context_missing" not in cleaned["flags"])
        # Relative first-person claims depend on the speaker.
        if re.search(r"\b(?:I|we|our|my)\b|我们|本人|我", cleaned["clean_text"], re.I):
            signature = (signature, post["username"].lower())
        if can_merge and signature in seen:
            duplicates += 1
            continue
        if can_merge:
            seen.add(signature)
        for index, part in enumerate(cleaned["fragments"]):
            record = {"id": post["id"] if index == 0 else f"{post['id']}.{index + 1}",
                      "account": source_label(post), "published_at": post["created_at"],
                      "part": index + 1, "text": post["text"][part["start"]:part["end"]] if evidence else part["text"]}
            if post.get("source_type") == "rss":
                record["content_kind"] = "RSS 标题与摘要"
            if not evidence:
                if part["links"]:
                    record["links"] = part["links"]
                if cleaned["flags"]:
                    record["quality_flags"] = [FLAG_LABELS.get(flag, flag) for flag in cleaned["flags"]]
            records.append(record)
    return records, duplicates


def prepare_posts(snapshot, evidence=False):
    """Deduplicate identical text; split long posts so extraction reads all text."""
    if "facts" in snapshot:
        duplicates = prepare_clean_posts(snapshot)[1] if snapshot["posts"] else 0
        return snapshot["evidence" if evidence else "records"], duplicates
    if snapshot.get("cleaner_version"):
        return prepare_clean_posts(snapshot, evidence)
    # Older in-progress jobs retain their original batch boundaries and IDs.
    seen, records, duplicates = set(), [], 0
    for post in snapshot["posts"]:
        text = post["text"]
        signature = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
        if signature in seen and text.strip():
            duplicates += 1
            continue
        seen.add(signature)
        raw = text
        part = 0
        while raw or part == 0:
            fragment = source_fragment(raw)
            source_id = post["id"] if part == 0 else f"{post['id']}.{part + 1}"
            records.append({"id": source_id, "account": "@" + post["username"],
                            "published_at": post["created_at"], "part": part + 1, "text": fragment})
            raw = raw[len(fragment):]
            part += 1
    return records, duplicates


class Workflow(ModelTasks):
    def __init__(self, store, run_id, client):
        self.store, self.run_id, self.client = store, run_id, client

    def call(self, key, instruction, payload, validate):
        return super().call(key, instruction, payload, validate,
                            model=MODEL, base_url=BASE_URL, api_key=API_KEY)

    def extract_group(self, key, day, group):
        ids = {p["id"] for p in group}
        result = None
        if not self.store.step(self.run_id, key + ":split"):
            try:
                result = self.call(key, EXTRACT, {"day": day, "posts": group},
                                   lambda v: extract_schema(v, ids, partial=len(group) > 1))
            except (OutputLimitError, CitationError):
                if len(group) <= 1:
                    raise
                self.store.save_step(self.run_id, key + ":split", {"enabled": True})
                logging.info("Daily report %s: splitting extraction batch %s after validation/output failure", self.run_id, key)
        if result is None:
            result = {"events": [], "ignored_ids": [], "missing_ids": sorted(ids)}
        missing = set(result.get("missing_ids", []))
        if missing:
            remaining = [p for p in group if p["id"] in missing]
            for index, subset in enumerate(batches(remaining, count_limit=max(1, len(group) // 2))):
                repaired = self.extract_group(f"{key}:missing:{index}", day, subset)
                result["events"].extend(repaired["events"])
                result["ignored_ids"].extend(repaired["ignored_ids"])
        return extract_schema(result, ids)

    def parallel(self, stage, items, operation):
        results = {}
        self.store.progress(self.run_id, stage, 0, len(items))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(operation, i, item): i for i, item in enumerate(items)}
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    self.store.progress(self.run_id, stage, len(results), len(items))
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        return [results[i] for i in range(len(items))]

    def run(self, job, base=BASE):
        snapshot = job["snapshot"]
        if snapshot is None:
            snapshot = snapshot_posts(job["day"], base)
            try:
                snapshot = EventStore(base, readonly=True).attach(snapshot)
            except ProcessingPending:
                if day_bounds(job["day"])[0].date() < datetime.now(TZ).date() - timedelta(days=1):
                    raise WorkflowError("该日期缺少已保存的事实处理结果；后台持续处理今天和昨天的内容") from None
                raise
            self.store.snapshot(self.run_id, snapshot, MODEL)
        posts = {p["id"]: p for p in snapshot["posts"]}
        records, duplicates = prepare_posts(snapshot)
        evidence_map = {record["id"]: record for record in prepare_posts(snapshot, evidence=True)[0]}
        groups = list(batches(records))
        candidates, ignored = [], set()
        def extract(index, group):
            return self.extract_group(f"extract:{index}", job["day"], group)
        if "facts" in snapshot:
            # Saved source-local facts are frozen with the edition. Repeated editions
            # only summarize, never extract old posts again or use future event state.
            event_groups = {}
            for fact in snapshot["facts"]:
                event_groups.setdefault(fact["event_id"], []).append({k: v for k, v in fact.items() if k != "event_id"})
            def merge_event(index, item):
                event_id, facts = item
                level = 0
                while len(facts) > 1:
                    merged = []
                    for number, group in enumerate(batches(facts, count_limit=40)):
                        if len(group) == 1:
                            merged.extend(group)
                            continue
                        ids = {i for e in group for i in e["source_ids"]}
                        result = self.call(f"select:event:{event_id}:{level}:{number}", SELECT.replace("LIMIT", "1"),
                            {"day": job["day"], "events": group}, lambda v: select_schema(v, ids, 1))
                        merged.extend(result["events"])
                    facts = merged
                    level += 1
                return facts
            for result in self.parallel("汇总已保存事件（1/3）", list(event_groups.items()), merge_event):
                candidates.extend(result)
            ignored = {p["id"] for p in records} - {i for e in snapshot["facts"] for i in e["source_ids"]}
        else:
            # Resume editions created by the previous pipeline from their checkpoints.
            for result in self.parallel("整理推文（1/3）", groups, extract):
                candidates.extend(result["events"])
                ignored.update(result["ignored_ids"])
        candidate_count = len(candidates)
        # A bounded reduction tree, preserving intermediate results and their citations.
        level = 0
        while candidates:
            chunks = list(batches(candidates, count_limit=40))
            final = len(chunks) == 1
            def select(index, group):
                ids = {i for e in group for i in e["source_ids"]}
                limit = min(MAX_EVENTS, len(group)) if final else min(6, max(1, len(group) // 2))
                return self.call(f"select:{level}:{index}", SELECT.replace("LIMIT", str(limit)),
                                   {"day": job["day"], "events": group},
                                   lambda v: select_schema(v, ids, limit))
            selected = self.parallel(f"筛选重点（2/3）· 第 {level + 1} 轮", chunks, select)
            candidates = [event for result in selected for event in result["events"]]
            if final:
                break
            level += 1
            if level > 12:
                raise WorkflowError("事件归并超出处理轮数，请缩小日报范围")
        verified, dropped = [], 0
        candidates.sort(key=lambda e: -e["importance"])
        def verify(index, candidate):
            sources = [evidence_map[i] for i in candidate["source_ids"]]
            # Original texts, never just previous model summaries, ground the final draft.
            evidence = [{"id": p["id"], "account": p["account"],
                         "published_at": p["published_at"], "text": p["text"],
                         "part": p["part"]} for p in sources]
            ids = {p["id"] for p in sources}
            def validate(value):
                if not isinstance(value, dict) or "event" not in value:
                    raise WorkflowError("模型未返回有效的核对结果")
                if value["event"] is None:
                    return {"event": None}
                return {"event": event_schema(value["event"], ids, timeline=True)}
            return self.call(f"verify:{index}", VERIFY, {"candidate": candidate, "sources": evidence}, validate)["event"]
        for event in self.parallel("核对并成稿（3/3）", candidates, verify):
            if event is None:
                dropped += 1
                continue
            # Model references identify the exact source fragment; links identify the post.
            event["source_ids"] = list(dict.fromkeys(i.split(".")[0] for i in event["source_ids"]))
            for entry in event["timeline"]:
                entry["source_ids"] = list(dict.fromkeys(i.split(".")[0] for i in entry["source_ids"]))
                entry["published_at"] = min(posts[i]["created_at"] for i in entry["source_ids"])
            event["timeline"].sort(key=lambda e: e["published_at"])
            event["first_reported_at"] = min(posts[i]["created_at"] for i in event["source_ids"])
            verified.append(event)
        verified.sort(key=lambda e: -e["importance"])
        self.store.complete(self.run_id, {"day": job["day"], "events": verified,
            "post_count": len(posts), "unique_count": len(posts) - duplicates,
            "duplicate_count": duplicates, "candidate_count": candidate_count,
            "ignored_count": len(ignored - {i for e in verified for i in e["source_ids"]}),
            "dropped_count": dropped, "generated_at": now_iso(), "timezone": "Asia/Shanghai"})


def process_job(store, job):
    try:
        with httpx.Client() as client:
            Workflow(store, job["id"], client).run(job)
        logging.info("Daily report %s completed", job["id"])
    except ProcessingPending as pending:
        store.defer(job["id"], "等待后台处理新帖", pending.done, pending.total)
    except Exception as error:
        message = str(error) if isinstance(error, WorkflowError) else "日报处理异常，请重试或查看服务日志"
        store.fail(job["id"], message)
        # Log the type only: exception strings from gateways can contain private URLs.
        logging.error("Daily report %s failed (%s): %s", job["id"], type(error).__name__, message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Queue a report for YYYY-MM-DD (Asia/Shanghai)")
    parser.add_argument("--once", action="store_true", help="Process queued reports and exit")
    parser.add_argument("--schedule", action="store_true", help="Queue yesterday's report after 08:00 Asia/Shanghai")
    args = parser.parse_args()
    if not BASE_URL or not API_KEY:
        parser.error("XMONITOR_AI_BASE_URL and XMONITOR_AI_API_KEY are required")
    os.umask(0o007)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = ReportStore()
    with (store.directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A daily-report worker is already running")
        store.recover()
        if args.date:
            store.enqueue(args.date)
        while True:
            if args.schedule:
                schedule_due(store)
            job = store.claim()
            if job:
                process_job(store, job)
            elif args.once:
                return
            else:
                time.sleep(5)


if __name__ == "__main__":
    main()
