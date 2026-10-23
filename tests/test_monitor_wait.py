"""Exercise quota waits without sending any requests to X."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import monitor
from twscrape.accounts_pool import NoAccountError


def account(*, active=True, **locks):
    return SimpleNamespace(active=active, locks=locks)


class QueueDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_earliest_active_session_wins_and_other_queues_are_independent(self):
        accounts = [account(active=False),
                    account(SearchTimeline=self.now + timedelta(minutes=8)),
                    account(SearchTimeline=self.now + timedelta(minutes=3))]
        self.assertEqual(monitor.queue_ready_at(accounts, "SearchTimeline", self.now),
                         self.now + timedelta(minutes=3, seconds=2))
        self.assertIsNone(monitor.queue_ready_at(accounts, "UserTweets", self.now))
        accounts.append(account())
        self.assertIsNone(monitor.queue_ready_at(accounts, "SearchTimeline", self.now))

    def test_expired_locks_and_second_precision_boundary(self):
        accounts = [account(SearchTimeline=self.now.replace(tzinfo=None))]
        self.assertEqual(monitor.queue_ready_at(accounts, "SearchTimeline", self.now),
                         self.now + timedelta(seconds=2))
        self.assertIsNone(monitor.queue_ready_at(accounts, "SearchTimeline",
                                                self.now + timedelta(seconds=3)))

    def test_missing_and_inactive_sessions_are_not_treated_as_rate_limits(self):
        for accounts in ([], [account(active=False)]):
            with self.assertRaises(NoAccountError):
                monitor.queue_ready_at(accounts, "SearchTimeline", self.now)


class MonitorWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.status_path = Path(self.directory.name) / "status.json"
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.status = {"tibo": {"checked_at": (self.now - timedelta(minutes=5)).isoformat(),
                                "new_count": 4, "error": None}}
        self.pending = ["tibo", "thsottiaux"]
        self.sleeps = []
        self.wait_snapshots = []
        self.clock = SimpleNamespace(now=lambda tz: self.now)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.status_path.exists():
            self.wait_snapshots.append(json.loads(self.status_path.read_text()))
        self.now += timedelta(seconds=seconds)

    async def test_waits_locally_until_reset_then_resumes_without_losing_last_success(self):
        pool = SimpleNamespace(get_all=AsyncMock(return_value=[
            account(SearchTimeline=self.now + timedelta(seconds=65))]))
        checked_at = self.status["tibo"]["checked_at"]
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep):
            ready = await monitor.wait_for_queue(pool, "SearchTimeline", self.status,
                                                 self.pending, self.status_path)
        self.assertTrue(ready)
        self.assertEqual(self.sleeps, [30, 30, 7])
        for saved in self.wait_snapshots:
            self.assertEqual(saved["tibo"]["checked_at"], checked_at)
            self.assertEqual(saved["tibo"]["new_count"], 4)
            self.assertIsNone(saved["tibo"]["error"])
            self.assertEqual(saved["thsottiaux"]["state"], "waiting")
        self.assertEqual(self.status["tibo"]["state"], "pending")
        self.assertNotIn("retry_at", self.status["tibo"])

    async def test_new_session_can_resume_before_original_deadline(self):
        pool = SimpleNamespace(get_all=AsyncMock(side_effect=[
            [account(SearchTimeline=self.now + timedelta(minutes=10))], [account()]]))
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep):
            self.assertTrue(await monitor.wait_for_queue(
                pool, "SearchTimeline", self.status, self.pending, self.status_path))
        self.assertEqual(self.sleeps, [30])

    async def test_removed_target_stops_waiting_without_fetching(self):
        pool = SimpleNamespace(get_all=AsyncMock(return_value=[
            account(SearchTimeline=self.now + timedelta(minutes=10))]))
        monitored = lambda: not self.sleeps
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep):
            self.assertFalse(await monitor.wait_for_queue(
                pool, "SearchTimeline", self.status, self.pending, self.status_path, monitored))
        self.assertEqual(pool.get_all.await_count, 1)

    async def test_session_expiry_during_wait_is_reported(self):
        pool = SimpleNamespace(get_all=AsyncMock(side_effect=[
            [account(SearchTimeline=self.now + timedelta(minutes=10))], [account(active=False)]]))
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep):
            with self.assertRaises(NoAccountError):
                await monitor.wait_for_queue(pool, "SearchTimeline", self.status,
                                             self.pending, self.status_path)
        self.assertEqual(self.sleeps, [30])

    async def test_mid_request_limit_retries_same_target_after_cooldown(self):
        login = account()
        api = SimpleNamespace(pool=SimpleNamespace(get_all=AsyncMock(return_value=[login])))
        output = Path(self.directory.name) / "tibo_posts.jsonl"
        output.touch()
        seen = {123}
        calls = []

        async def poll(api, name, limit, path, seen_ids):
            calls.append(name)
            self.assertIs(seen_ids, seen)
            if len(calls) == 1:
                login.locks["SearchTimeline"] = self.now + timedelta(seconds=65)
                raise NoAccountError("No account available for queue SearchTimeline")
            self.assertGreater(self.now, login.locks["SearchTimeline"])
            return 3

        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep), patch.object(monitor, "poll", poll):
            result = await monitor.poll_with_wait(api, "tibo", 20, output, seen,
                                                  self.status, self.pending, self.status_path)
        self.assertEqual(result, 3)
        self.assertEqual(calls, ["tibo", "tibo"])
        self.assertEqual(self.sleeps, [30, 30, 7, 2])

    async def test_startup_respects_persisted_queue_lock_before_first_request(self):
        api = SimpleNamespace(pool=SimpleNamespace(get_all=AsyncMock(return_value=[
            account(SearchTimeline=self.now + timedelta(seconds=40))])))
        output = Path(self.directory.name) / "tibo_posts.jsonl"
        output.touch()
        poll = AsyncMock(return_value=0)
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep), patch.object(monitor, "poll", poll):
            self.assertEqual(await monitor.poll_with_wait(
                api, "tibo", 20, output, set(), self.status, self.pending, self.status_path), 0)
        self.assertEqual(self.sleeps, [30, 12])
        self.assertEqual(poll.await_count, 1)

    async def test_baseline_checks_both_timeline_queues(self):
        api = SimpleNamespace(pool=SimpleNamespace(get_all=AsyncMock(return_value=[
            account(UserTweets=self.now + timedelta(seconds=40))])))
        output = Path(self.directory.name) / "new_posts.jsonl"
        with patch.object(monitor, "datetime", self.clock), \
                patch.object(monitor.asyncio, "sleep", self.sleep), \
                patch.object(monitor, "poll", AsyncMock(return_value=4)):
            self.assertEqual(await monitor.poll_with_wait(
                api, "tibo", 100, output, set(), self.status, self.pending, self.status_path), 4)
        self.assertEqual(self.sleeps, [30, 12])
        self.assertEqual(self.wait_snapshots[0]["tibo"]["wait_queue"], "UserTweets")

    async def test_main_keeps_remaining_targets_and_waits_a_full_cycle_after_cooldown(self):
        class EndCycle(Exception):
            pass

        elapsed = 0
        calls = []

        async def poll(*args):
            nonlocal elapsed
            calls.append(args[1])
            elapsed += 700
            return 2

        async def end_cycle(seconds):
            self.assertEqual(seconds, 300)
            raise EndCycle

        argv = ["monitor.py", "--username", "tibo", "--username", "thsottiaux",
                "--status", str(self.status_path)]
        with patch("sys.argv", argv), patch.object(monitor, "API"), \
                patch.object(monitor, "output_for", lambda name, base: Path(self.directory.name) / name), \
                patch.object(monitor, "poll_with_wait", poll), \
                patch.object(monitor.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: elapsed)), \
                patch.object(monitor.asyncio, "sleep", end_cycle):
            with self.assertRaises(EndCycle):
                await monitor.main()
        self.assertEqual(calls, ["tibo", "thsottiaux"])
        saved = json.loads(self.status_path.read_text())
        self.assertIsNone(saved["thsottiaux"]["error"])
        self.assertEqual(saved["tibo"]["new_count"], 2)

    async def test_main_clears_wait_indicators_if_all_sessions_expire(self):
        class EndCycle(Exception):
            pass

        monitor.set_queue_wait(self.status, self.pending, "SearchTimeline",
                               self.now + timedelta(minutes=10), self.status_path)
        argv = ["monitor.py", "--username", "tibo", "--username", "thsottiaux",
                "--status", str(self.status_path)]
        with patch("sys.argv", argv), patch.object(monitor, "API"), \
                patch.object(monitor, "output_for", lambda name, base: Path(self.directory.name) / name), \
                patch.object(monitor, "poll_with_wait", AsyncMock(side_effect=NoAccountError("No active X login session"))), \
                patch.object(monitor.asyncio, "sleep", AsyncMock(side_effect=EndCycle)):
            with self.assertRaises(EndCycle):
                await monitor.main()
        for state in json.loads(self.status_path.read_text()).values():
            self.assertIn("登录会话不可用", state["error"])
            self.assertNotEqual(state.get("state"), "waiting")
            self.assertNotIn("retry_at", state)


if __name__ == "__main__":
    unittest.main()
