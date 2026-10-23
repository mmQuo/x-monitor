"""Incrementally preprocess today's and yesterday's saved posts (Beijing time)."""

import argparse
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import post_cleaner as cleaner
from clean_store import BASE, CleanStore, load_links
from report_store import TZ
from sources import active_source_names, source_path


def signature(path, cutoff, links_hash):
    stat = path.stat()
    return cleaner.digest([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                           cutoff.isoformat(), cleaner.CLEANER_VERSION, links_hash])


def process_once(base=BASE, store=None, now=None):
    base = Path(base)
    store = store or CleanStore(base)
    now = (now or datetime.now(TZ)).astimezone(TZ)
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    links = load_links(base)
    links_hash = cleaner.digest(links)
    stats = {"files": 0, "cleaned": 0, "cached": 0, "invalid": 0}
    names = {name.lower() for name in active_source_names(base)}
    for name in sorted(names):
        path = source_path(base, name)
        if not path.exists():
            continue
        before = signature(path, cutoff, links_hash)
        file_key = str(path.relative_to(base))
        if store.file_current(file_key, before):
            continue
        stats["files"] += 1
        pending, partial, future = [], False, False
        def flush():
            _, counts = store.prepare(pending, links)
            for key in counts:
                stats[key] += counts[key]
            pending.clear()
        with path.open("rb") as file:
            for raw in file:
                # A concurrent append may leave an unfinished final line.
                if not raw.endswith(b"\n"):
                    partial = True
                    continue
                try:
                    post = json.loads(raw)
                    stamp = datetime.fromisoformat(post["created_at"])
                    if stamp.tzinfo is None:
                        raise ValueError("Missing timezone")
                    if stamp < cutoff:
                        continue
                    if stamp > now:
                        future = True
                        continue
                    post = cleaner.source_post(dict(post, username=name))
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    stats["invalid"] += 1
                    continue
                pending.append(post)
                if len(pending) >= 200:
                    flush()
        flush()
        if not partial and not future and signature(path, cutoff, links_hash) == before:
            store.file_done(file_key, before)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    os.umask(0o007)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = CleanStore(args.base)
    with (store.directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A preprocessing worker is already running")
        while True:
            try:
                stats = process_once(args.base, store)
                if args.once:
                    print(json.dumps({"pass": stats, "cache": store.stats()}, ensure_ascii=False))
                    return
                if stats["files"]:
                    logging.info("Preprocessing: %s", json.dumps(stats))
            except Exception as error:
                # Post contents and private URLs never go into service logs.
                logging.error("Preprocessing failed (%s)", type(error).__name__)
                if args.once:
                    raise SystemExit(1) from None
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
