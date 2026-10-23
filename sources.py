"""Source identities shared by collection, enrichment, reports and the dashboard."""

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from link_utils import safe_url

RSS_FEEDS = {
    "wsj_markets": {"label": "华尔街日报 · 市场", "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"},
    "fed_policy": {"label": "美联储 · 货币政策", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml"},
    "fed_speeches": {"label": "美联储 · 官员讲话", "url": "https://www.federalreserve.gov/feeds/speeches.xml"},
}
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")


def content_id(value):
    value = str(value)
    if re.fullmatch(r"[0-9]{1,25}", value):
        return str(int(value))
    if re.fullmatch(r"rss:[0-9a-f]{64}", value):
        return value
    raise ValueError("Invalid content ID")


def rss_sources(base, include_disabled=False):
    try:
        config = json.loads((Path(base) / "config/rss_sources.json").read_text())
    except FileNotFoundError:
        config = {"enabled": []}
    enabled = config.get("enabled", [])
    if not isinstance(enabled, list) or any(name not in RSS_FEEDS for name in enabled):
        raise ValueError("Invalid RSS source configuration")
    return [{"name": "rss:" + key, "label": feed["label"], "url": feed["url"],
             "kind": "rss", "enabled": key in enabled}
            for key, feed in RSS_FEEDS.items() if include_disabled or key in enabled]


def set_rss_enabled(base, key, enabled):
    if key not in RSS_FEEDS or not isinstance(enabled, bool):
        raise ValueError("Unknown RSS source")
    directory = Path(base) / "config"
    with (directory / ".rss.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = {source["name"][4:] for source in rss_sources(base)}
        (names.add if enabled else names.discard)(key)
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as file:
            json.dump({"enabled": sorted(names)}, file)
            temp = Path(file.name)
        try:
            os.chmod(temp, 0o660)
            os.replace(temp, directory / "rss_sources.json")
        finally:
            temp.unlink(missing_ok=True)


def active_source_names(base):
    path = Path(base) / "config/targets.txt"
    lines = path.read_text().splitlines() if path.exists() else []
    names = [line.strip().lstrip("@") for line in lines
             if HANDLE.fullmatch(line.strip().lstrip("@"))]
    names += [source["name"] for source in rss_sources(base)]
    return list({name.lower(): name for name in names}.values())


def source_path(base, name):
    if name.startswith("rss:") and name[4:] in RSS_FEEDS:
        return Path(base) / "rss_data" / f"{name[4:]}_posts.jsonl"
    if HANDLE.fullmatch(name):
        return Path(base) / f"{name.lower()}_posts.jsonl"
    raise ValueError("Invalid source name")


def source_label(post):
    name = str(post.get("username", ""))
    if post.get("source_type") == "rss" or name.startswith("rss:"):
        return str(post.get("source_label") or RSS_FEEDS.get(name[4:], {}).get("label", "RSS"))
    return "@" + name


def article_url(post):
    if post.get("source_type") == "rss" or str(post.get("username", "")).startswith("rss:"):
        return safe_url(post.get("url")) or ""
    name = str(post.get("username", post.get("account", "")))
    return safe_url(post.get("url")) or f"https://x.com/{name}/status/{content_id(post['id'])}"
