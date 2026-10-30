"""Continuously extract new facts, maintain events and publish live rankings."""

import argparse
import fcntl
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ai_worker import API_KEY, BASE_URL, MODEL
from event_prompts import FACTS, ROUTE, UPDATE
from event_store import BASE, EventStore
from model_tasks import (ModelTasks, WorkflowError, OutputLimitError, CitationError,
                         batches, byte_len, clip, event_schema, extract_schema, PROMPT_BYTES)
from post_cleaner import digest
from report_store import TZ, now_iso
from sources import active_source_names


def extraction_version():
    return digest(["source-facts-v1", FACTS, MODEL, BASE_URL])


def cutoff_at(now):
    return (now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=1)).astimezone(timezone.utc).isoformat()


def sync_cleaned(base, store, version, now):
    path = Path(os.getenv("XMONITOR_CLEAN_DIR") or Path(base) / "clean_data") / "clean_posts.sqlite3"
    if not path.exists():
        return 0
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("""SELECT * FROM clean_posts
            WHERE json_extract(raw_post,'$.created_at')>=? AND json_extract(raw_post,'$.created_at')<=?""",
            (cutoff_at(now), now.astimezone(timezone.utc).isoformat())).fetchall()
    finally:
        db.close()
    return store.ingest(rows, version, {name.lower() for name in active_source_names(base)})


def fact_schema(value, records):
    result = extract_schema(value, set(records))
    used = set()
    for fact, original in zip(result["events"], value["events"]):
        if len(fact["source_ids"]) != 1:
            raise WorkflowError("每项事实必须只引用自身的一条输入")
        source = fact["source_ids"][0]
        quote = original.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in records[source]["text"]:
            raise WorkflowError("事实依据必须逐字摘录自身输入的连续原文，保留原来的换行、标点和大小写")
        if len(quote) > 400:
            raise WorkflowError(f"事实摘录有{len(quote)}个字符，须缩至400个字符以内")
        entities = original.get("entities")
        if (not isinstance(entities, list) or len(entities) > 8 or
                any(not isinstance(e, str) or not e.strip() or len(e) > 100 for e in entities)):
            raise WorkflowError("事实主体列表无效")
        fact.update(quote=quote, entities=list(dict.fromkeys(entities)))
        used.add(source)
    if used & set(result["ignored_ids"]):
        raise WorkflowError("有实质事实的输入不能同时忽略")
    return result


def terms(value):
    text = " ".join([value.get("title", ""), value.get("summary", ""), *value.get("entities", [])]).lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", text))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(segment[i:i + 2] for i in range(len(segment) - 1))
    return words - {"the", "and", "for", "with", "that", "this", "from", "公司", "表示", "发布", "宣布"}


def route_payload(facts, catalog):
    """Retrieve candidates with entity overlap and rare bilingual terms; the model decides identity."""
    catalog_terms = {item["id"]: terms({**item["profile"], **(item["body"] or {})}) for item in catalog}
    freq = Counter(term for values in catalog_terms.values() for term in values)
    scores = {}
    for fact in facts:
        query = terms(fact["body"])
        entities = {e.casefold() for e in fact["body"]["entities"]}
        matches = []
        for item in catalog:
            overlap = query & catalog_terms[item["id"]]
            common_entities = entities & {e.casefold() for e in item["profile"].get("entities", [])}
            score = sum(math.log(1 + len(catalog) / freq[t]) for t in overlap) + 8 * len(common_entities)
            if score > 0:
                matches.append((score, item["id"]))
        for score, event_id in sorted(matches, reverse=True)[:5]:
            scores[event_id] = max(scores.get(event_id, 0), score)
    candidates = sorted((item for item in catalog if item["id"] in scores), key=lambda item: -scores[item["id"]])[:24]
    fact_ids = {f"F{i + 1}": fact["id"] for i, fact in enumerate(facts)}
    event_ids = {}
    payload = {"facts": [{"id": alias, "published_at": fact["published_at"],
                **{k: fact["body"][k] for k in ("title", "summary", "entities")}}
                for alias, fact in zip(fact_ids, facts)], "catalog": []}
    for item in candidates:
        alias = f"E{len(event_ids) + 1}"
        body = item["body"] or item["profile"]
        entry = {"id": alias, "title": body["title"], "summary": clip(body["summary"], 600),
                 "last_progress": item["progress_at"], "entities": item["profile"].get("entities", [])[:8]}
        if byte_len({**payload, "catalog": payload["catalog"] + [entry]}) > 15500:
            break
        payload["catalog"].append(entry)
        event_ids[alias] = item["id"]
    return payload, fact_ids, event_ids


def route_schema(value, fact_ids, event_ids):
    if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
        raise WorkflowError("事件分组无效")
    groups, seen_facts, seen_events = [], set(), set()
    for group in value["groups"]:
        if not isinstance(group, dict):
            raise WorkflowError("事件分组无效")
        fs, es = group.get("fact_ids"), group.get("event_ids")
        if (not isinstance(fs, list) or not fs or not isinstance(es, list) or
            any(not isinstance(f, str) or f not in fact_ids for f in fs) or
            any(not isinstance(e, str) or e not in event_ids for e in es)):
            raise WorkflowError("事件分组包含无效编号")
        if len(fs) != len(set(fs)) or len(es) != len(set(es)) or seen_facts & set(fs) or seen_events & set(es):
            raise WorkflowError("每条事实和已有事件只能出现在一个分组")
        seen_facts.update(fs)
        seen_events.update(es)
        groups.append({"fact_ids": [fact_ids[f] for f in fs], "event_ids": [event_ids[e] for e in es]})
    if seen_facts != set(fact_ids):
        raise WorkflowError("仍有新事实未归入事件")
    return {"groups": groups}


class EventProcessor:
    def __init__(self, store, client, version=None):
        self.store = store
        self.version = version or extraction_version()
        self.tasks = ModelTasks(store, self.version, client)

    def extract(self, rows):
        records = [row["record"] for row in rows]
        key = "facts:" + digest([row["key"] for row in rows])
        try:
            result = self.tasks.call(key, FACTS, {"posts": records},
                lambda value: fact_schema(value, {r["id"]: r for r in records}))
        except WorkflowError as error:
            if len(rows) == 1 or re.search(r"HTTP|超时|网络", str(error)):
                raise
            half = len(rows) // 2
            failures = []
            for subset in (rows[:half], rows[half:]):
                try:
                    self.extract(subset)
                except WorkflowError as failure:
                    failures.append(failure)
            if failures:
                raise failures[0]
            return
        self.store.save_facts(rows, result)

    def route(self, facts, catalog):
        payload, fact_ids, event_ids = route_payload(facts, catalog)
        if len(facts) == 1 and not event_ids:
            groups = [{"fact_ids": [facts[0]["id"]], "event_ids": []}]
        else:
            result = self.tasks.call("route:" + digest([ROUTE, payload, fact_ids, event_ids]), ROUTE, payload,
                lambda value: route_schema(value, fact_ids, event_ids))
            groups = result["groups"]
        self.store.route(groups, facts)

    def summarize(self, event):
        facts = self.store.facts(event["id"])
        active = {fact["id"] for fact in facts}
        covered = set(event["covered"])
        current = event["body"] if covered <= active and not event["invalidated"] else None
        if current is None:
            covered = set()
        added = [fact for fact in facts if fact["id"] not in covered]
        progress = event["progress_at"] if current else (facts[0]["published_at"] if facts else event["progress_at"])
        by_source = {fact["record"]["id"]: fact["evidence"] for fact in facts}
        old_copies = {(fact["cleaned"]["duplicate_group"], fact["raw_post"]["id"])
                      for fact in facts if fact["id"] in covered and fact["cleaned"]["duplicate_group"]}
        # Identical copies add provenance without resetting the event's age.
        added = [f for f in added if not any(g == f["cleaned"]["duplicate_group"] and p != f["raw_post"]["id"]
                                            for g, p in old_copies)]
        if current is None and len(added) == 1:
            fact = added.pop()
            current = {k: v for k, v in fact["body"].items() if k not in ("quote", "entities")}
            text = current["summary"] if len(current["summary"]) <= 240 else current["title"]
            current["timeline"] = [{"text": text, "source_ids": current["source_ids"]}]
            progress = fact["published_at"]
        limit = min(18000, PROMPT_BYTES - len(UPDATE.encode()) - 700)
        while added:
            group = []
            def payload_for(items):
                events = [{k: v for k, v in f["body"].items() if k not in ("quote", "entities")} for f in items]
                ids = list(dict.fromkeys((current or {}).get("source_ids", []) + [i for f in events for i in f["source_ids"]]))
                return {"candidate": current, "events": events,
                        "sources": [{k: by_source[i][k] for k in ("id", "account", "published_at", "text", "part")} for i in ids]}
            while added and byte_len(payload_for(group + added[:1])) <= limit:
                group.append(added.pop(0))
            if not group:
                raise WorkflowError("事件汇总材料超过上下文预算")
            payload = payload_for(group)
            allowed = {p["id"] for p in payload["sources"]}
            def validate(value):
                if not isinstance(value, dict) or not isinstance(value.get("changed"), bool):
                    raise WorkflowError("事件更新结果无效")
                item = event_schema(value.get("event"), allowed, timeline=True)
                if len(item["source_ids"]) > 3:
                    raise WorkflowError("当前事件摘要最多引用3条核心来源")
                return {"event": item, "changed": value["changed"]}
            result = self.tasks.call("update:" + digest([UPDATE, event["id"], payload]), UPDATE, payload, validate)
            if current is None or result["changed"]:
                progress = max(progress, *(f["published_at"] for f in group))
                current = result["event"]
        self.store.save_event(event["id"], current, sorted(active), progress)
        if current:
            profile = {**current, "entities": list(dict.fromkeys(e for f in facts for e in f["body"]["entities"]))[:24]}
            with self.store.connect() as db:
                db.execute("UPDATE events SET profile=? WHERE id=?", (json.dumps(profile, ensure_ascii=False), event["id"]))

    def process_once(self, base=BASE, now=None):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = cutoff_at(now)
        self.store.set_meta("heartbeat", now_iso())
        self.store.set_meta("extractor_version", self.version)
        synthesis_version = digest([ROUTE, UPDATE, MODEL])
        if self.store.meta("synthesis_version") != synthesis_version:
            with self.store.connect() as db:
                db.execute("UPDATE events SET dirty=1,invalidated=1,retry_at=0 WHERE replaced_by IS NULL AND progress_at>=?", (cutoff,))
            self.store.set_meta("synthesis_version", synthesis_version)
        synced = sync_cleaned(base, self.store, self.version, now)
        rows = self.store.pending_records(cutoff)
        # Batch by actual wire records, not the internal stored metadata.
        groups, group = [], []
        for row in rows:
            if group and (len(group) >= 10 or byte_len([r["record"] for r in group + [row]]) > 15500):
                groups.append(group)
                group = []
            group.append(row)
        if group:
            groups.append(group)
        errors = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self.extract, group): group for group in groups}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    message = str(error) if isinstance(error, WorkflowError) else "事实处理暂缓，后台会自动重试"
                    self.store.fail_records(futures[future], message)
                    errors.append(message)
        horizon = (now - timedelta(days=7)).isoformat()
        if self.store.meta("route_retry", 0) <= time.time():
            for _ in range(4):
                facts = self.store.facts(cutoff=cutoff, limit=10)
                if not facts:
                    break
                try:
                    catalog = self.store.catalog(horizon)
                    while len(facts) > 1 and byte_len(route_payload(facts, catalog)[0]) > 15500:
                        facts.pop()
                    self.route(facts, catalog)
                except Exception as error:
                    errors.append(str(error) if isinstance(error, WorkflowError) else "事件归并暂缓，后台会自动重试")
                    self.store.set_meta("route_retry", time.time() + 60)
                    break
        dirty = self.store.dirty(horizon)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(self.summarize, event): event for event in dirty}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    self.store.fail_event(futures[future]["id"])
                    errors.append(str(error) if isinstance(error, WorkflowError) else "事件更新暂缓，后台会自动重试")
        self.store.set_meta("heartbeat", now_iso())
        self.store.set_meta("error", errors[0] if errors else "")
        return {"ingested": synced, "records": len(rows), "events": len(dirty), "errors": len(errors)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 1 or not API_KEY or not BASE_URL:
        parser.error("A positive interval and configured AI endpoint are required")
    os.umask(0o007)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = EventStore(args.base)
    with (store.directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("An event worker is already running")
        with httpx.Client() as client:
            processor = EventProcessor(store, client)
            while True:
                try:
                    result = processor.process_once(args.base)
                    if any(result.values()):
                        logging.info("Event processing: %s", json.dumps(result))
                    if args.once:
                        return
                except Exception as error:
                    logging.error("Event processing failed (%s)", type(error).__name__)
                    store.set_meta("error", "事件处理暂缓，后台会自动重试")
                    if args.once:
                        raise SystemExit(1) from None
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
