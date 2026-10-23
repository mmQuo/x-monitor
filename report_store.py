"""Persistent daily-report jobs, snapshots and resumable workflow steps."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
REPORT_DIR = Path(os.getenv("XMONITOR_REPORT_DIR", str(BASE / "report_data")))
TZ = ZoneInfo("Asia/Shanghai")
ACTIVE = ("queued", "running")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(TZ).date()


def parse_day(value):
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("请选择有效日期") from None
    if day.isoformat() != value or day > today():
        raise ValueError("日期不能晚于今天")
    return day


def day_bounds(value):
    day = parse_day(value)
    start = datetime.combine(day, datetime.min.time(), TZ)
    return start, start + timedelta(days=1)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ReportStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else REPORT_DIR
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o770)
        self.path = self.directory / "reports.sqlite3"
        # SQLite defaults to 0644 even with a shared-group umask. Pre-create with
        # group write so the web user and worker can also write WAL/SHM files.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o660)
        except FileExistsError:
            pass
        else:
            try:
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT '等待处理',
                    done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    finished_at TEXT, error TEXT NOT NULL DEFAULT '',
                    snapshot TEXT, result TEXT, model TEXT NOT NULL DEFAULT '',
                    calls INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_day ON runs(day)
                    WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS runs_day ON runs(day, id DESC);
                CREATE TABLE IF NOT EXISTS steps (
                    run_id INTEGER NOT NULL REFERENCES runs(id), key TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY (run_id, key)
                );
                CREATE TABLE IF NOT EXISTS scheduled (day TEXT PRIMARY KEY);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, day, scheduled=False):
        parse_day(day)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if scheduled:
                if db.execute("SELECT 1 FROM scheduled WHERE day=?", (day,)).fetchone():
                    return None
            active = db.execute("SELECT id FROM runs WHERE day=? AND status IN ('queued','running')", (day,)).fetchone()
            if active:
                return active["id"]
            if scheduled:
                db.execute("INSERT INTO scheduled VALUES (?)", (day,))
                completed = db.execute("SELECT snapshot FROM runs WHERE day=? AND status='complete' ORDER BY id DESC LIMIT 1", (day,)).fetchone()
                if completed and completed["snapshot"]:
                    cutoff = json.loads(completed["snapshot"])["captured_at"]
                    if datetime.fromisoformat(cutoff) >= day_bounds(day)[1]:
                        return None
            pending = db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
            if pending >= 14:
                raise ValueError("已有较多日报等待生成，请稍后再试")
            stamp = now_iso()
            latest = db.execute("SELECT id, status,snapshot FROM runs WHERE day=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
            partial = bool(latest and latest["snapshot"] and
                datetime.fromisoformat(json.loads(latest["snapshot"])["captured_at"]) < day_bounds(day)[1])
            if latest and latest["status"] == "failed" and not (scheduled and partial):
                db.execute("UPDATE runs SET status='queued',stage='等待重试',error='',updated_at=? WHERE id=?", (stamp, latest["id"]))
                return latest["id"]
            cur = db.execute("INSERT INTO runs(day,created_at,updated_at) VALUES (?,?,?)", (day, stamp, stamp))
            return cur.lastrowid

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE runs SET status='running',stage='读取当日推文',updated_at=? WHERE id=?", (now_iso(), row["id"]))
        return self.get(row["id"])

    def recover(self):
        """Called only after the worker has acquired its exclusive process lock."""
        with self.connect() as db:
            db.execute("UPDATE runs SET status='queued',stage='等待恢复',updated_at=? WHERE status='running'", (now_iso(),))

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("snapshot", "result"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def latest(self, day, completed=False):
        with self.connect() as db:
            row = db.execute("SELECT id FROM runs WHERE day=?" + (" AND status='complete'" if completed else "") + " ORDER BY id DESC LIMIT 1", (day,)).fetchone()
        return self.get(row["id"]) if row else None

    def history(self, limit=30):
        with self.connect() as db:
            rows = db.execute("""SELECT id,day,status,stage,updated_at FROM runs
                WHERE id IN (SELECT max(id) FROM runs GROUP BY day) ORDER BY day DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def status(self, day):
        with self.connect() as db:
            row = db.execute("SELECT id,status,stage,done,total,updated_at FROM runs WHERE day=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
        return dict(row) if row else {}

    def progress(self, run_id, stage, done=0, total=0):
        with self.connect() as db:
            db.execute("UPDATE runs SET stage=?,done=?,total=?,updated_at=? WHERE id=?", (stage, done, total, now_iso(), run_id))

    def snapshot(self, run_id, snapshot, model):
        with self.connect() as db:
            db.execute("UPDATE runs SET snapshot=?,model=?,updated_at=? WHERE id=?", (encode(snapshot), model, now_iso(), run_id))

    def step(self, run_id, key):
        with self.connect() as db:
            row = db.execute("SELECT result FROM steps WHERE run_id=? AND key=?", (run_id, key)).fetchone()
        return json.loads(row["result"]) if row else None

    def save_step(self, run_id, key, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?)", (run_id, key, encode(result)))
            db.execute("UPDATE runs SET updated_at=? WHERE id=?", (now_iso(), run_id))

    def usage(self, run_id, usage):
        def count(key):
            value = usage.get(key, 0)
            return value if isinstance(value, int) and value >= 0 else 0
        with self.connect() as db:
            db.execute("UPDATE runs SET calls=calls+1,input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?", (count("prompt_tokens"), count("completion_tokens"), run_id))

    def complete(self, run_id, result):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='complete',stage='已完成',result=?,error='',finished_at=?,updated_at=? WHERE id=?", (encode(result), now_iso(), now_iso(), run_id))

    def fail(self, run_id, message):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='failed',stage='生成中断',error=?,updated_at=? WHERE id=?", (message[:240], now_iso(), run_id))


def schedule_due(store, now=None):
    local = (now or datetime.now(TZ)).astimezone(TZ)
    if local.hour >= 8:
        try:
            return store.enqueue((local.date() - timedelta(days=1)).isoformat(), scheduled=True)
        except ValueError:
            # Drain a full manual queue first, then retry the automatic edition.
            return None
    return None
