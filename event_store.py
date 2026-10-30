"""Versioned facts and persistent events shared by live rankings and reports."""

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from post_cleaner import FLAG_LABELS, digest, source_post
from report_store import TZ, encode, now_iso
from sources import article_url, source_label

BASE = Path(__file__).resolve().parent


class ProcessingPending(Exception):
    def __init__(self, done, total):
        self.done, self.total = done, total
        super().__init__(f"等待后台处理新帖 · {done}/{total}")


def post_records(post, cleaned):
    result = []
    for index, part in enumerate(cleaned["fragments"]):
        source = post["id"] if index == 0 else f"{post['id']}.{index + 1}"
        record = {"id": source, "account": source_label(post),
                  "published_at": post["created_at"], "part": index + 1, "text": part["text"]}
        if part["links"]:
            record["links"] = part["links"]
        if cleaned["flags"]:
            record["quality_flags"] = [FLAG_LABELS.get(f, f) for f in cleaned["flags"]]
        if post.get("source_type") == "rss":
            record["content_kind"] = "RSS 标题与摘要"
        evidence = {**record, "text": post["text"][part["start"]:part["end"]]}
        result.append((record, evidence))
    return result


class EventStore:
    def __init__(self, base=BASE, directory=None, readonly=False):
        self.directory = Path(directory or os.getenv("XMONITOR_EVENT_DIR") or Path(base) / "event_data")
        self.path = self.directory / "events.sqlite3"
        self.readonly = readonly
        if readonly:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o770)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o660)
        except FileExistsError:
            pass
        else:
            os.fchmod(fd, 0o660)
            os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inputs (
                    key TEXT PRIMARY KEY, post_id TEXT NOT NULL, raw_hash TEXT NOT NULL,
                    version TEXT NOT NULL, source TEXT NOT NULL, published_at TEXT NOT NULL,
                    raw_post TEXT NOT NULL, cleaned TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS inputs_post ON inputs(post_id,raw_hash,version);
                CREATE INDEX IF NOT EXISTS inputs_recent ON inputs(active,published_at);
                CREATE UNIQUE INDEX IF NOT EXISTS inputs_current ON inputs(post_id) WHERE active=1;
                CREATE TABLE IF NOT EXISTS records (
                    key TEXT PRIMARY KEY, input_key TEXT NOT NULL REFERENCES inputs(key),
                    source_id TEXT NOT NULL, record TEXT NOT NULL, evidence TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS records_input ON records(input_key);
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, record_key TEXT NOT NULL REFERENCES records(key),
                    body TEXT NOT NULL, event_id TEXT
                );
                CREATE INDEX IF NOT EXISTS facts_record ON facts(record_key);
                CREATE INDEX IF NOT EXISTS facts_event ON facts(event_id);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                    covered TEXT NOT NULL DEFAULT '[]', progress_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, dirty INTEGER NOT NULL DEFAULT 1,
                    invalidated INTEGER NOT NULL DEFAULT 0, replaced_by TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    scope TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(scope,key)
                );
                CREATE TABLE IF NOT EXISTS usage (
                    scope TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro" if self.readonly else self.path,
                             uri=self.readonly, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, key, default=None):
        if not self.path.exists():
            return default
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))

    def ingest(self, rows, version, names):
        added = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                post, cleaned = json.loads(row["raw_post"]), json.loads(row["result"])
                if post["username"].lower() not in names:
                    continue
                key = digest([row["raw_hash"], row["link_hash"], row["cleaner_version"], version])
                current = db.execute("SELECT key FROM inputs WHERE post_id=? AND active=1", (post["id"],)).fetchone()
                if current and current[0] == key:
                    continue
                if current:
                    self._invalidate(db, current[0])
                db.execute("INSERT OR IGNORE INTO inputs VALUES (?,?,?,?,?,?,?,?,1)",
                    (key, post["id"], row["raw_hash"], version, post["username"].lower(),
                     post["created_at"], encode(post), encode(cleaned)))
                db.execute("UPDATE inputs SET active=1 WHERE key=?", (key,))
                for record, evidence in post_records(post, cleaned):
                    db.execute("INSERT OR IGNORE INTO records(key,input_key,source_id,record,evidence) VALUES (?,?,?,?,?)",
                        (digest([key, record["id"]]), key, record["id"], encode(record), encode(evidence)))
                added += 1
            for row in db.execute("SELECT key,source FROM inputs WHERE active=1").fetchall():
                if row["source"] not in names:
                    self._invalidate(db, row["key"])
        return added

    @staticmethod
    def _invalidate(db, key):
        db.execute("""UPDATE events SET dirty=1,invalidated=1,retry_at=0 WHERE id IN (
            SELECT f.event_id FROM facts f JOIN records r ON r.key=f.record_key WHERE r.input_key=?)""", (key,))
        db.execute("UPDATE inputs SET active=0 WHERE key=?", (key,))

    def pending_records(self, cutoff, limit=24):
        with self.connect() as db:
            rows = db.execute("""SELECT r.* FROM records r JOIN inputs i ON i.key=r.input_key
                WHERE i.active=1 AND i.published_at>=? AND r.state='pending' AND r.retry_at<=?
                ORDER BY i.published_at DESC,r.source_id LIMIT ?""", (cutoff, time.time(), limit)).fetchall()
        return [{**dict(row), "record": json.loads(row["record"]), "evidence": json.loads(row["evidence"])} for row in rows]

    def save_facts(self, rows, result):
        grouped = {row["source_id"]: [] for row in rows}
        for fact in result["events"]:
            grouped[fact["source_ids"][0]].append(fact)
        with self.connect() as db:
            for row in rows:
                for index, fact in enumerate(grouped[row["source_id"]]):
                    db.execute("INSERT OR IGNORE INTO facts(id,record_key,body) VALUES (?,?,?)",
                        (digest([row["key"], index]), row["key"], encode(fact)))
                db.execute("UPDATE records SET state='done',error='',retry_at=0 WHERE key=?", (row["key"],))

    def fail_records(self, rows, error):
        with self.connect() as db:
            for row in rows:
                delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
                db.execute("UPDATE records SET attempts=attempts+1,error=?,retry_at=? WHERE key=? AND state='pending'",
                           (error[:240], time.time() + delay, row["key"]))

    def facts(self, event_id=None, cutoff=None, limit=None):
        query = """SELECT f.*,i.published_at,i.raw_post,i.cleaned,r.record,r.evidence FROM facts f
            JOIN records r ON r.key=f.record_key JOIN inputs i ON i.key=r.input_key WHERE i.active=1"""
        args = []
        if event_id is None:
            query += " AND f.event_id IS NULL"
        else:
            query += " AND f.event_id=?"
            args.append(event_id)
        if cutoff:
            query += " AND i.published_at>=?"
            args.append(cutoff)
        query += " ORDER BY i.published_at,f.id"
        if limit:
            query += " LIMIT ?"
            args.append(limit)
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [{**dict(row), **{key: json.loads(row[key]) for key in
                ("body", "raw_post", "cleaned", "record", "evidence")}} for row in rows]

    def catalog(self, cutoff):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE replaced_by IS NULL AND progress_at>=?", (cutoff,)).fetchall()
        return [{**dict(row), "profile": json.loads(row["profile"]),
                 "body": json.loads(row["body"]) if row["body"] else None,
                 "covered": json.loads(row["covered"])} for row in rows]

    def route(self, groups, facts):
        by_id = {fact["id"]: fact for fact in facts}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for group in groups:
                members = [by_id[key] for key in group["fact_ids"]]
                ids = group["event_ids"]
                event_id = ids[0] if ids else digest(["event", members[0]["id"]])[:24]
                if not ids:
                    first = members[0]
                    db.execute("INSERT OR IGNORE INTO events(id,profile,progress_at,updated_at) VALUES (?,?,?,?)",
                        (event_id, encode(first["body"]), first["published_at"], now_iso()))
                for other in ids[1:]:
                    db.execute("UPDATE facts SET event_id=? WHERE event_id=?", (event_id, other))
                    db.execute("UPDATE events SET replaced_by=? WHERE id=?", (event_id, other))
                for member in members:
                    db.execute("UPDATE facts SET event_id=? WHERE id=? AND event_id IS NULL", (event_id, member["id"]))
                db.execute("UPDATE events SET dirty=1,retry_at=0 WHERE id=?", (event_id,))

    def dirty(self, cutoff, limit=24):
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM events WHERE dirty=1 AND replaced_by IS NULL AND retry_at<=?
                AND (progress_at>=? OR invalidated=1) ORDER BY progress_at DESC LIMIT ?""",
                (time.time(), cutoff, limit)).fetchall()
        return [{**dict(row), "profile": json.loads(row["profile"]),
                 "body": json.loads(row["body"]) if row["body"] else None,
                 "covered": json.loads(row["covered"])} for row in rows]

    def save_event(self, event_id, body, covered, progress_at):
        with self.connect() as db:
            db.execute("""UPDATE events SET body=?,covered=?,progress_at=?,updated_at=?,
                dirty=0,invalidated=0,attempts=0,retry_at=0 WHERE id=?""",
                (encode(body) if body else '', encode(covered), progress_at, now_iso(), event_id))

    def fail_event(self, event_id):
        with self.connect() as db:
            row = db.execute("SELECT attempts FROM events WHERE id=?", (event_id,)).fetchone()
            db.execute("UPDATE events SET attempts=attempts+1,retry_at=? WHERE id=?",
                       (time.time() + min(3600, 30 * 2 ** min(row[0], 7)), event_id))

    # ModelTasks' shared persistence interface. No report/run dependency.
    def step(self, scope, key):
        with self.connect() as db:
            row = db.execute("SELECT result FROM tasks WHERE scope=? AND key=?", (scope, key)).fetchone()
        return json.loads(row[0]) if row else None

    def save_step(self, scope, key, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?)", (scope, key, encode(result)))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat',?)", (encode(now_iso()),))

    def usage(self, scope, usage):
        counts = [usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")]
        counts = [v if isinstance(v, int) and v >= 0 else 0 for v in counts]
        with self.connect() as db:
            db.execute("""INSERT INTO usage VALUES (?,1,?,?) ON CONFLICT(scope) DO UPDATE SET
                calls=calls+1,input_tokens=input_tokens+excluded.input_tokens,
                output_tokens=output_tokens+excluded.output_tokens""", (scope, *counts))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat',?)", (encode(now_iso()),))

    def attach(self, snapshot):
        """Freeze saved facts and exact evidence; never clean, enqueue or call a model."""
        posts = snapshot["posts"]
        if not posts:
            return {**snapshot, "records": [], "evidence": [], "facts": []}
        if not self.path.exists():
            raise ProcessingPending(0, len(posts))
        records, evidence, facts, frozen = [], [], [], []
        version = self.meta("extractor_version")
        with self.connect() as db:
            for post in posts:
                cleaned = post.get("cleaned")
                if not cleaned:
                    continue
                key = digest([cleaned["raw_hash"], cleaned["link_hash"], cleaned["cleaner_version"], version])
                row = db.execute("SELECT * FROM inputs WHERE key=?", (key,)).fetchone()
                if not row:
                    continue
                parts = db.execute("SELECT * FROM records WHERE input_key=?", (row["key"],)).fetchall()
                atoms = db.execute("""SELECT f.* FROM facts f JOIN records r ON r.key=f.record_key
                    WHERE r.input_key=? ORDER BY f.id""", (row["key"],)).fetchall()
                if any(r["state"] != 'done' for r in parts) or any(not f["event_id"] for f in atoms):
                    continue
                frozen.append({**post, "cleaned": json.loads(row["cleaned"]), "processing_key": row["key"]})
                records.extend(json.loads(r["record"]) for r in parts)
                evidence.extend(json.loads(r["evidence"]) for r in parts)
                for atom in atoms:
                    fact = json.loads(atom["body"])
                    facts.append({**{k: v for k, v in fact.items() if k not in ("quote", "entities")},
                                  "event_id": atom["event_id"]})
        if len(frozen) != len(posts):
            raise ProcessingPending(len(frozen), len(posts))
        return {**snapshot, "posts": frozen, "records": records, "evidence": evidence,
                "facts": facts, "extractor_version": version}

    def status(self, now=None):
        now = now or datetime.now(timezone.utc)
        cutoff = (now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                  - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        if not self.path.exists():
            return {"pending": 0, "failed": 0, "ready": 0, "heartbeat": None}
        with self.connect() as db:
            row = db.execute("""SELECT count(*) AS total,
                sum(EXISTS(SELECT 1 FROM records r WHERE r.input_key=i.key AND r.state!='done') OR
                    EXISTS(SELECT 1 FROM records r JOIN facts f ON f.record_key=r.key WHERE r.input_key=i.key AND f.event_id IS NULL)) AS pending,
                sum(EXISTS(SELECT 1 FROM records r WHERE r.input_key=i.key AND r.state!='done' AND r.attempts>0)) AS failed
                FROM inputs i WHERE active=1 AND published_at>=?""", (cutoff,)).fetchone()
            updating = db.execute("SELECT count(*) FROM events WHERE dirty=1 AND replaced_by IS NULL AND progress_at>=?", (cutoff,)).fetchone()[0]
        return {"pending": row["pending"] or 0, "failed": row["failed"] or 0,
                "ready": row["total"] - (row["pending"] or 0), "updating": updating,
                "heartbeat": self.meta("heartbeat"), "error": self.meta("error", "")}

    def top(self, now=None, limit=10):
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (now - timedelta(hours=24)).isoformat()
        if not self.path.exists():
            return []
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM events WHERE replaced_by IS NULL AND invalidated=0
                AND body!='' AND progress_at>=? AND progress_at<=?""", (cutoff, now.isoformat())).fetchall()
            ranked = []
            for row in rows:
                item = json.loads(row["body"])
                age = max(0, (now - datetime.fromisoformat(row["progress_at"])).total_seconds() / 3600)
                score = item["importance"] * (0.7 + 0.3 * math.exp(-age / 12))
                ranked.append({**item, "id": row["id"], "score": score,
                    "progress_at": row["progress_at"], "updated_at": row["updated_at"], "updating": bool(row["dirty"])})
            ranked.sort(key=lambda e: (e["score"], e["progress_at"], e["id"]), reverse=True)
            result = []
            for item in ranked:
                sources = {}
                for source_id in item["source_ids"]:
                    source = db.execute("""SELECT i.raw_post FROM inputs i JOIN records r ON r.input_key=i.key
                        WHERE i.active=1 AND r.source_id=?""", (source_id,)).fetchone()
                    if source:
                        post = json.loads(source[0])
                        sources[source_id] = {**post, "url": article_url(post)}
                if set(sources) != set(item["source_ids"]):
                    continue
                item["sources"] = list({post["id"]: post for post in sources.values()}.values())
                numbers = {p["id"]: n + 1 for n, p in enumerate(item["sources"])}
                for post in sources.values():
                    post["number"] = numbers[post["id"]]
                for entry in item["timeline"]:
                    entry["sources"] = list({sources[i]["id"]: sources[i] for i in entry["source_ids"]}.values())
                    entry["published_at"] = min(p["created_at"] for p in entry["sources"])
                item["timeline"].sort(key=lambda e: e["published_at"])
                result.append(item)
                if len(result) >= limit:
                    break
        return result
