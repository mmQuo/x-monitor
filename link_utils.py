"""Normalize X short links and render safe, readable links."""

import re
from urllib.parse import urlsplit

from markupsafe import Markup, escape

TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
TRAILING = ".,;:!?)]}，。；：！？）】"


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 33 or char in '<>"\'' for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return value


def tweet_links(tweet) -> list[dict[str, str]]:
    result = []
    for link in getattr(tweet, "links", []):
        short = safe_url(link.tcourl)
        expanded = safe_url(link.url)
        if short and expanded and TCO_RE.fullmatch(short):
            result.append({"short": short, "expanded": expanded,
                           "display": str(link.text or "")})
    return result


def links_for_post(post: dict, cache: dict[str, str]) -> dict[str, str]:
    result = {}
    for short in TCO_RE.findall(str(post.get("text", ""))):
        expanded = safe_url(cache.get(short))
        if expanded:
            result[short] = expanded
    for item in post.get("links") or []:
        if not isinstance(item, dict):
            continue
        short = safe_url(item.get("short"))
        expanded = safe_url(item.get("expanded"))
        if short and expanded and TCO_RE.fullmatch(short):
            result[short] = expanded
    return result


def link_label(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "链接"
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    text = host + path
    return text[:54] + "…" if len(text) > 55 else text


def linkify(value: object, links: dict[str, str] | None = None) -> Markup:
    source = str(value or "")
    links = links or {}
    parts = []
    previous = 0
    for match in URL_RE.finditer(source):
        raw = match.group()
        url = raw.rstrip(TRAILING)
        if not url:
            continue
        parts.append(escape(source[previous:match.start()]))
        target = safe_url(links.get(url)) or safe_url(url)
        if target:
            label = link_label(target) if TCO_RE.fullmatch(url) and target != url else url
            parts.append(Markup('<a href="%s" target="_blank" rel="noopener noreferrer" title="%s">%s</a>')
                         % (escape(target), escape(target), escape(label)))
        else:
            parts.append(escape(url))
        previous = match.start() + len(url)
    parts.append(escape(source[previous:]))
    return Markup("").join(parts)
