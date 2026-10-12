"""Resolve historical t.co links without following third-party destinations."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

from link_utils import TCO_RE, safe_url

BASE = Path(__file__).resolve().parent
CACHE = BASE / "link_data" / "links.json"


def load_cache() -> dict[str, str]:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def pending_links(cache: dict[str, str]) -> list[str]:
    short_urls = set()
    for path in BASE.glob("*_posts.jsonl"):
        with path.open(encoding="utf-8") as file:
            for line in file:
                try:
                    post = json.loads(line)
                except ValueError:
                    continue
                short_urls.update(TCO_RE.findall(str(post.get("text", ""))))
                for item in post.get("links") or []:
                    if not isinstance(item, dict):
                        continue
                    short, expanded = item.get("short"), safe_url(item.get("expanded"))
                    if short and expanded and TCO_RE.fullmatch(short):
                        cache[short] = expanded
    return sorted(short_urls - cache.keys())


def save_cache(cache: dict[str, str]) -> None:
    CACHE.parent.mkdir(mode=0o750, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CACHE.parent, delete=False) as file:
        json.dump(cache, file, ensure_ascii=False, separators=(",", ":"))
        temp = Path(file.name)
    os.chmod(temp, 0o640)
    os.replace(temp, CACHE)


async def resolve(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, short: str):
    async with semaphore:
        try:
            response = await client.head(short, follow_redirects=False)
            target = safe_url(response.headers.get("location"))
            if not target:
                response = await client.get(short, follow_redirects=False)
                target = safe_url(response.headers.get("location"))
            return short, target if target and target != short else None
        except httpx.HTTPError:
            return short, None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    original = load_cache()
    cache = original.copy()
    pending = pending_links(cache)
    if args.limit:
        pending = pending[:args.limit]
    print(f"Resolving {len(pending)} t.co links", flush=True)
    if not pending:
        if cache != original:
            save_cache(cache)
        print(f"Cached {len(cache)} total", flush=True)
        return
    semaphore = asyncio.Semaphore(args.concurrency)
    resolved = 0
    last_saved = 0
    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
        tasks = [resolve(client, semaphore, short) for short in pending]
        for completed in asyncio.as_completed(tasks):
            short, target = await completed
            if target:
                cache[short] = target
                resolved += 1
            if resolved - last_saved >= 50:
                save_cache(cache)
                last_saved = resolved
    if cache != original:
        save_cache(cache)
    print(f"Resolved {resolved}/{len(pending)}; cached {len(cache)} total", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
