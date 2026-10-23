"""Poll an X account's posts with twscrape and append new ones to JSONL."""

import argparse
import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from twscrape import API
from twscrape.accounts_pool import NoAccountError

from link_utils import tweet_links


HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")


def targets(usernames: list[str], targets_file: Path | None) -> list[str]:
    names = list(usernames)
    if targets_file is not None:
        with targets_file.open(encoding="utf-8") as file:
            names.extend(line.strip() for line in file if line.strip() and not line.lstrip().startswith("#"))
    result = []
    seen = set()
    for name in names:
        name = name.lstrip("@").strip()
        if not HANDLE.fullmatch(name):
            raise ValueError(f"Invalid X username: {name!r}")
        if name.lower() not in seen:
            result.append(name)
            seen.add(name.lower())
    return result


def output_for(username: str, output_dir: Path) -> Path:
    return output_dir / f"{username.lower()}_posts.jsonl"


def save_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(status, file, ensure_ascii=False)
        temp_path = Path(file.name)
    os.chmod(temp_path, 0o640)
    os.replace(temp_path, path)


def existing_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as file:
        return {int(row["id"]) for line in file if (row := json.loads(line.strip()))}


def baseline_cutoff(path: Path) -> datetime | None:
    """Keep search results from before the first check out of later polls."""
    marker = path.with_suffix(".baseline_at")
    try:
        return datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    try:
        with path.open(encoding="utf-8") as file:
            observed = [
                datetime.fromisoformat(row["observed_at"])
                for line in file
                if (row := json.loads(line.strip())).get("baseline") and row.get("observed_at")
            ]
        return min(observed) if observed else None
    except (OSError, ValueError):
        return None


async def poll(api: API, username: str, limit: int, output: Path, seen: set[int]) -> int:
    baseline = not output.exists()
    cutoff = datetime.now(timezone.utc) if baseline else baseline_cutoff(output)
    if cutoff is None:
        raise RuntimeError(f"Missing initial check time for @{username}")
    user = await api.user_by_login(username) if baseline else None
    if baseline and user is None:
        raise RuntimeError(f"Could not resolve @{username}; check the account and X session")

    # Search has a separate, larger rate-limit bucket and one page covers a 5-minute poll.
    source = api.user_tweets(user.id, limit=limit) if user else api.search(f"from:{username}", limit=min(limit, 20))
    posts = []
    async for tweet in source:
        # The timeline can include reposts; keep posts authored by this account.
        authored = tweet.user.id == user.id if user else tweet.user.username.lower() == username.lower()
        if authored and tweet.retweetedTweet is None:
            posts.append(tweet)

    newest_saved_id = max(seen, default=0)
    fresh = sorted(
        (
            post for post in posts
            if post.id not in seen
            and (baseline or (post.id > newest_saved_id and post.date > cutoff))
        ),
        key=lambda post: post.id,
    )
    if baseline:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".baseline_at").write_text(cutoff.isoformat(), encoding="utf-8")
        output.touch()
    if fresh:
        with output.open("a", encoding="utf-8") as file:
            for post in fresh:
                record = {
                    "id": post.id,
                    "username": username,
                    "url": post.url,
                    "created_at": post.date.isoformat(),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "text": post.rawContent,
                    "links": tweet_links(post),
                    "has_media": any(getattr(post.media, key, []) for key in ("photos", "videos", "animated")),
                    "reply_to_id": str(post.inReplyToTweetId) if post.inReplyToTweetId else None,
                    "quoted_post_id": str(post.quotedTweet.id) if post.quotedTweet else ("unknown" if post.isQuoteStatus else None),
                    "baseline": baseline,
                }
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
                file.flush()
                seen.add(post.id)
                if not baseline:
                    print(f"NEW {post.url}\n{post.rawContent}\n", flush=True)

    logging.info("Checked @%s: %d posts, %d new%s", username, len(posts), len(fresh),
                 " (initial baseline)" if baseline else "")
    return len(fresh)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor new posts from an X account")
    parser.add_argument("--username", action="append", default=[], help="Account to monitor; repeatable")
    parser.add_argument("--targets-file", type=Path, help="One X username per line; reloaded each check")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between checks")
    parser.add_argument("--limit", type=int, default=100, help="Recent posts to inspect per check")
    parser.add_argument("--db", type=Path, default=Path("accounts.db"))
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="Directory for per-account JSONL files")
    parser.add_argument("--status", type=Path, default=Path("monitor_status.json"))
    parser.add_argument("--once", action="store_true", help="Run one check, then exit")
    args = parser.parse_args()
    os.umask(0o027)
    if args.interval < 1 or args.limit < 1:
        parser.error("--interval and --limit must be positive")
    if not args.username and args.targets_file is None:
        parser.error("--username or --targets-file is required")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    api = API(str(args.db), raise_when_no_account=True, wait_timeout=60)
    if not any(account.active for account in await api.pool.get_all()):
        parser.error(f"No active X account in {args.db}; add cookies with twscrape first")

    seen_by_output: dict[Path, set[int]] = {}
    try:
        status = json.loads(args.status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    cycle_number = 0
    while True:
        cycle_start = asyncio.get_running_loop().time()
        try:
            usernames = targets(args.username, args.targets_file)
        except Exception:
            logging.exception("Could not read target accounts")
            if args.once:
                raise
            await asyncio.sleep(args.interval)
            continue

        if usernames:
            shift = cycle_number % len(usernames)
            usernames = usernames[shift:] + usernames[:shift]
        cycle_number += 1
        for username in usernames:
            output = output_for(username, args.output_dir)
            try:
                if output not in seen_by_output:
                    seen_by_output[output] = existing_ids(output)
                seen = seen_by_output[output]
                new_count = await poll(api, username, args.limit, output, seen)
                status[username.lower()] = {
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "new_count": new_count,
                    "error": None,
                }
                save_status(args.status, status)
            except Exception as error:
                status[username.lower()] = {
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "new_count": 0,
                    "error": f"{type(error).__name__}: {str(error)[:200]}",
                }
                save_status(args.status, status)
                logging.exception("Check failed for @%s", username)
                if args.once:
                    raise
                if isinstance(error, NoAccountError):
                    logging.warning("X rate limit reached; remaining accounts will resume next cycle")
                    break
        if args.once:
            break
        elapsed = asyncio.get_running_loop().time() - cycle_start
        await asyncio.sleep(max(0, args.interval - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
