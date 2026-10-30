"""Versioned preprocessing cache, shared by the cleaner and daily-report worker."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import post_cleaner as cleaner
from link_utils import links_for_post, safe_url, TCO_RE

BASE = Path(__file__).resolve().parent


def load_links(base):
    try:
        value = json.loads((Path(base) / "link_data/links.json").read_text())
        if isinstance(value, dict):
            return {key: target for key, target in value.items()
                    if TCO_RE.fullmatch(key) and safe_url(target)}
    except (OSError, ValueError):
        pass
    return {}


def read_cleaned(base, posts):
    """Read matching results without starting preprocessing or creating a database."""
    path = Path(os.getenv("XMONITOR_CLEAN_DIR") or Path(base) / "clean_data") / "clean_posts.sqlite3"
    if not path.exists():
        return {}
    results = {}
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        for post in posts:
            row = db.execute("SELECT raw_hash,result FROM clean_posts WHERE post_id=?", (post["id"],)).fetchone()
            if row and row[0] == cleaner.digest(cleaner.source_post(post)):
                results[post["id"]] = json.loads(row[1])
    finally:
        db.close()
    return results


class CleanStore:
    def __init__(self, base=BASE, directory=None):
        self.directory = Path(directory or os.getenv("XMONITOR_CLEAN_DIR") or Path(base) / "clean_data")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o770)
        self.path = self.directory / "clean_posts.sqlite3"
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
                CREATE TABLE IF NOT EXISTS clean_posts (
                    post_id TEXT PRIMARY KEY, raw_hash TEXT NOT NULL,
                    link_hash TEXT NOT NULL, cleaner_version TEXT NOT NULL,
                    duplicate_group TEXT, raw_post TEXT NOT NULL,
                    result TEXT NOT NULL, processed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS clean_duplicate_group ON clean_posts(duplicate_group);
                CREATE TABLE IF NOT EXISTS clean_files (
                    name TEXT PRIMARY KEY, signature TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, posts, links=None):
        results, stats = {}, {"cleaned": 0, "cached": 0}
        with self.connect() as db:
            for value in posts:
                post = cleaner.source_post(value)
                raw_hash = cleaner.digest(post)
                resolved = links_for_post(post, links or {})
                link_hash = cleaner.digest(resolved)
                cached = db.execute("SELECT * FROM clean_posts WHERE post_id=?", (post["id"],)).fetchone()
                if (cached and cached["raw_hash"] == raw_hash and cached["link_hash"] == link_hash
                        and cached["cleaner_version"] == cleaner.CLEANER_VERSION):
                    results[post["id"]] = json.loads(cached["result"])
                    stats["cached"] += 1
                    continue
                result = cleaner.clean_post(post, resolved)
                stamp = datetime.now(timezone.utc).isoformat()
                result["processed_at"] = stamp
                db.execute("""INSERT INTO clean_posts VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        raw_hash=excluded.raw_hash, link_hash=excluded.link_hash,
                        cleaner_version=excluded.cleaner_version,
                        duplicate_group=excluded.duplicate_group,
                        raw_post=excluded.raw_post, result=excluded.result,
                        processed_at=excluded.processed_at""",
                    (post["id"], raw_hash, link_hash, cleaner.CLEANER_VERSION,
                     result["duplicate_group"], cleaner.encode(post), cleaner.encode(result), stamp))
                results[post["id"]] = result
                stats["cleaned"] += 1
        return results, stats

    def file_current(self, name, signature):
        with self.connect() as db:
            row = db.execute("SELECT signature FROM clean_files WHERE name=?", (name,)).fetchone()
        return bool(row and row["signature"] == signature)

    def file_done(self, name, signature):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO clean_files VALUES (?,?)", (name, signature))

    def stats(self):
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM clean_posts").fetchone()[0]
            groups = db.execute("""SELECT count(*),coalesce(sum(n-1),0) FROM (
                SELECT count(*) AS n FROM clean_posts WHERE duplicate_group IS NOT NULL
                GROUP BY duplicate_group HAVING count(*)>1)""").fetchone()
            flags = {}
            for row in db.execute("SELECT result FROM clean_posts"):
                for flag in json.loads(row[0])["flags"]:
                    flags[flag] = flags.get(flag, 0) + 1
        return {"posts": total, "duplicate_groups": groups[0], "matching_posts": groups[1], "flags": flags}
