"""Conservative, deterministic preprocessing of saved posts."""

import hashlib
import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from link_utils import URL_RE, links_for_post, safe_url
from sources import RSS_FEEDS, content_id

CLEANER_VERSION = "1"
SOURCE_BYTES = 3000
PART_BYTES = 5500
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
FLAG_LABELS = {"greeting": "问候", "promotion": "推广提示", "link_only": "仅有链接",
               "empty_text": "正文为空", "context_missing": "上下文不足"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
CODE = re.compile(r"`|(?m:^[ \t]*(?:~~~)|^(?: {4}|\t)\S)")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def source_post(post):
    """Canonicalize metadata for cache keys, retaining exact original text."""
    post_id = content_id(post["id"])
    name = str(post["username"])
    stamp = datetime.fromisoformat(post["created_at"])
    rss = name.startswith("rss:") and name[4:] in RSS_FEEDS
    if (not rss and not HANDLE.fullmatch(name)) or stamp.tzinfo is None:
        raise ValueError("Invalid post metadata")
    if post_id.startswith("rss:") != rss:
        raise ValueError("Mismatched source identity")
    result = {"id": post_id, "username": name.lower(),
              "created_at": stamp.astimezone(timezone.utc).isoformat(),
              "text": str(post.get("text") or ""),
              "links": post.get("links") if isinstance(post.get("links"), list) else []}
    for key in ("reply_to_id", "quoted_post_id", "has_media"):
        if key in post:
            result[key] = post[key]
    if rss:
        url = safe_url(post.get("url"))
        if not url:
            raise ValueError("Missing article URL")
        result.update(source_type="rss", source_label=RSS_FEEDS[name[4:]]["label"],
                      url=url, content_kind="feed_summary")
    return result


def normalize_text(text):
    text = unicodedata.normalize("NFC", html.unescape(text)).replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL.sub("", text)
    lines, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            lines.append(line)
        elif fenced or line.startswith(("    ", "\t")) or "`" in line:
            lines.append(line)
        else:
            lines.append(re.sub(r"[^\S\n]+", " ", line).strip())
    # Preserve code-block blank lines as well as indentation.
    result, blanks, fenced = [], 0, False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        blanks = blanks + 1 if not line else 0
        if fenced or blanks < 2:
            result.append(line)
    return "\n".join(result).strip("\n")


def canonical_url(url):
    """Comparison key only; original and expanded addresses remain available."""
    parts = urlsplit(url)
    query = []
    for parameter in parts.query.split("&"):
        key = unquote_plus(parameter.partition("=")[0]).lower()
        if key.startswith("utm_") or key in TRACKING_KEYS:
            continue
        query.append(parameter)
    return urlunsplit(parts._replace(query="&".join(query)))


def trim_url(value):
    value = value.rstrip(".,;:!?，。；：！？")
    for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def text_and_links(raw, resolved):
    text, links = normalize_text(raw), []
    if CODE.search(raw):
        return text, links
    labels = {}
    def replace(match):
        original = trim_url(match.group())
        expanded = safe_url(resolved.get(original)) or safe_url(original)
        if not expanded:
            return match.group()
        if original not in labels:
            number = len(links) + 1
            while f"[链接{number}]" in text or f"链接{number}" in labels.values():
                number += 1
            label = f"链接{number}"
            labels[original] = label
            links.append({"label": label, "original": original, "expanded": expanded,
                          "canonical": canonical_url(expanded)})
        return f"[{labels[original]}]" + match.group()[len(original):]
    return URL_RE.sub(replace, text), links


def quality_flags(text, links, post):
    body = text
    for link in links:
        body = body.replace(f"[{link['label']}]", "")
    body = body.strip()
    flags = []
    if not body:
        flags.append("link_only" if links else "empty_text")
    greeting = re.fullmatch(r"(?:gm|gn|good morning|good night|早安|晚安|早上好)[\s.!！。☀🌞🌙️]*", body, re.I)
    if greeting:
        flags.append("greeting")
    if re.search(r"\b(?:giveaway|raffle)\b|抽奖", body, re.I) and re.search(r"\b(?:follow|retweet|enter to win)\b|关注|转发", body, re.I):
        flags.append("promotion")
    if (not body or greeting or post.get("reply_to_id") or post.get("quoted_post_id") or
        re.fullmatch(r"(?:同意|确实|这个厉害|赞|agree|agreed|this|nice)[\s.!！。]*", body, re.I)):
        flags.append("context_missing")
    return flags


def clip(text, size):
    return text.encode()[:size].decode("utf-8", errors="ignore")


def fragments(raw, resolved):
    """Bound each model input and freeze offsets into the exact original text."""
    result, start = [], 0
    preserve_code = bool(CODE.search(raw))
    while start < len(raw) or not result:
        fragment = clip(raw[start:], SOURCE_BYTES)
        while True:
            text, links = (fragment, []) if preserve_code else text_and_links(fragment, resolved)
            # Link URLs are context hints; complete addresses live in the cache.
            hints = [{"label": link["label"], "url": clip(link["canonical"], 320)} for link in links]
            part = {"start": start, "end": start + len(fragment), "text": text, "links": hints}
            if len(encode(fragment).encode()) <= SOURCE_BYTES and len(encode(part).encode()) <= PART_BYTES:
                break
            fragment = fragment[:max(1, len(fragment) * 3 // 4)]
        result.append(part)
        start += len(fragment)
        if not fragment:
            break
    return result


def clean_post(post, resolved=None):
    post = source_post(post)
    resolved = links_for_post(post, resolved or {})
    text, links = text_and_links(post["text"], resolved)
    flags = quality_flags(text, links, post)
    # This identifies matching content, not independent corroboration or an event.
    duplicate_group = digest([text, [link["canonical"] for link in links]]) if text.strip() else None
    if post.get("source_type") == "rss" and text.strip():
        # Recurring official releases can reuse the same title and short summary.
        duplicate_group = digest([text, canonical_url(post["url"])])
    return {"post_id": post["id"], "raw_hash": digest(post), "link_hash": digest(resolved),
            "cleaner_version": CLEANER_VERSION, "clean_text": text, "links": links,
            "flags": flags, "duplicate_group": duplicate_group,
            "fragments": fragments(post["text"], resolved)}
