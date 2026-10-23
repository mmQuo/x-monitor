"""Collect configured RSS feeds into the same content pipeline as X posts."""

import argparse
import calendar
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx

from link_utils import safe_url
from sources import rss_sources, source_path

BASE = Path(__file__).resolve().parent
MAX_BYTES = 2_000_000
INTERVAL = 300
TZ = ZoneInfo("Asia/Shanghai")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "noscript"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return "\n".join(line for raw in "".join(parser.parts).splitlines()
                     if (line := re.sub(r"\s+", " ", raw).strip()))


def identity_url(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"mod", "fbclid", "gclid"}]
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def parse_feed(data, source, now):
    feed = feedparser.parse(data)
    if not feed.version or (feed.bozo and not feed.entries):
        raise ValueError("Invalid RSS or Atom response")
    posts, invalid = [], 0
    for item in feed.entries:
        try:
            url = safe_url(urljoin(source["url"], item.get("link", ""))) if item.get("link") else None
            parsed_date = item.get("published_parsed") or item.get("updated_parsed")
            if not url or not parsed_date:
                raise ValueError("Missing URL or publication time")
            stamp = datetime.fromtimestamp(calendar.timegm(parsed_date), timezone.utc)
            title, summary = plain_text(item.get("title")), plain_text(item.get("summary"))
            if not title:
                raise ValueError("Missing title")
            text = title + ("\n\n" + summary if summary and summary != title else "")
            # URL identity survives tracking-parameter and feed GUID changes.
            post_id = "rss:" + hashlib.sha256(identity_url(url).encode()).hexdigest()
            posts.append({"id": post_id, "username": source["name"], "source_type": "rss",
                          "source_label": source["label"], "feed_url": source["url"],
                          "content_kind": "feed_summary", "title": title, "url": url,
                          "created_at": stamp.isoformat(), "observed_at": now.isoformat(),
                          "text": text, "links": [], "has_media": False,
                          "reply_to_id": None, "quoted_post_id": None, "baseline": False})
        except (ValueError, TypeError, OverflowError):
            invalid += 1
    return posts, invalid


def load_status(base):
    try:
        return json.loads((Path(base) / "rss_data/status.json").read_text())
    except FileNotFoundError:
        return {}


def save_status(base, state):
    directory = Path(base) / "rss_data"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as file:
        json.dump(state, file, ensure_ascii=False)
        temp = Path(file.name)
    try:
        os.chmod(temp, 0o640)
        os.replace(temp, directory / "status.json")
    finally:
        temp.unlink(missing_ok=True)


def fetch_feed(client, url, headers):
    # The curated registry fixes upstream hosts; redirects stay on that host.
    host = urlsplit(url).hostname
    for _ in range(4):
        with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            if response.status_code == 304:
                return 304, b"", response.headers
            if response.is_redirect:
                target = urljoin(url, response.headers.get("location", ""))
                parts = urlsplit(target)
                if parts.scheme != "https" or parts.hostname != host or parts.port not in (None, 443):
                    raise ValueError("RSS redirect outside configured host")
                url = target
                continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("RSS response too large")
            return response.status_code, bytes(data), response.headers
    raise ValueError("Too many RSS redirects")


def process_once(base=BASE, client=None, now=None, force=False):
    base = Path(base)
    (base / "rss_data").mkdir(mode=0o750, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    state = load_status(base)
    stats = {"checked": 0, "new": 0, "failed": 0, "invalid": 0}
    if client is None:
        with httpx.Client(timeout=25, headers={"User-Agent": "XMonitorRSS/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml"}) as owned:
            return process_once(base, owned, now, force)
    for source in rss_sources(base):
        current = dict(state.get(source["name"], {}))
        if not force and current.get("next_check_at") and now < datetime.fromisoformat(current["next_check_at"]):
            continue
        headers = {}
        if current.get("etag"):
            headers["If-None-Match"] = current["etag"]
        if current.get("last_modified"):
            headers["If-Modified-Since"] = current["last_modified"]
        try:
            code, data, response_headers = fetch_feed(client, source["url"], headers)
            fresh = []
            if code != 304:
                posts, invalid = parse_feed(data, source, now)
                stats["invalid"] += invalid
                cutoff = datetime.fromisoformat(current["since"]) if current.get("since") else (
                    now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
                path = source_path(base, source["name"])
                seen = set()
                if path.exists():
                    with path.open("r+b") as file:
                        complete_bytes = 0
                        for line in file:
                            if not line.endswith(b"\n"):
                                # Recover an interrupted final append; this worker
                                # owns the single-writer lock for the RSS directory.
                                file.truncate(complete_bytes)
                                break
                            if line.strip():
                                seen.add(json.loads(line)["id"])
                            complete_bytes += len(line)
                for post in sorted(posts, key=lambda p: (p["created_at"], p["id"])):
                    stamp = datetime.fromisoformat(post["created_at"])
                    if cutoff <= stamp <= now and post["id"] not in seen:
                        fresh.append(post)
                        seen.add(post["id"])
                with path.open("a", encoding="utf-8") as file:
                    for post in fresh:
                        file.write(json.dumps(post, ensure_ascii=False) + "\n")
                    file.flush()
                    os.fsync(file.fileno())
                current["since"] = cutoff.isoformat()
                if posts:
                    current["latest_published_at"] = max(p["created_at"] for p in posts)
                future = any(datetime.fromisoformat(p["created_at"]) > now for p in posts)
                current["etag"] = None if future else response_headers.get("etag")
                current["last_modified"] = None if future else response_headers.get("last-modified")
            current.update(checked_at=now.isoformat(), new_count=len(fresh), error=None,
                           failures=0, next_check_at=(now + timedelta(seconds=INTERVAL)).isoformat())
            stats["checked"] += 1
            stats["new"] += len(fresh)
        except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as error:
            failures = current.get("failures", 0) + 1
            delay = min(3600, INTERVAL * 2 ** min(failures - 1, 4))
            detail = f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
            current.update(error=f"订阅读取失败（{detail}），稍后自动重试", failures=failures,
                           next_check_at=(now + timedelta(seconds=delay)).isoformat())
            stats["failed"] += 1
            logging.warning("RSS source %s failed (%s)", source["name"], detail)
        state[source["name"]] = current
        save_status(base, state)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o027)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = args.base / "rss_data"
    directory.mkdir(mode=0o750, exist_ok=True)
    with (directory / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            stats = process_once(args.base, force=args.once)
            if stats["checked"] or stats["failed"]:
                logging.info("RSS cycle: %s", stats)
            if args.once:
                print(json.dumps(stats))
                return
            time.sleep(30)


if __name__ == "__main__":
    main()
