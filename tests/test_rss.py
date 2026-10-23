"""RSS collection and downstream integration using isolated data and fake HTTP."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault("XMONITOR_WEB_SECRET", "rss-integration-test")
os.environ.setdefault("XMONITOR_WEB_USERNAME", "test")
os.environ.setdefault("XMONITOR_WEB_PASSWORD_HASH", "unused")

import ai_worker
import clean_worker
import daily_worker
import post_cleaner
import rss_worker
import sources
import web_app

NOW = datetime(2026, 1, 3, 4, tzinfo=timezone.utc)


def feed(*items):
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>'
            + ''.join(f'<item><title>{title}</title><link>{url}</link><guid>{guid}</guid>'
                      f'<description><![CDATA[{body}]]></description><pubDate>{stamp}</pubDate></item>'
                      for title, url, guid, body, stamp in items) + '</channel></rss>').encode()


ITEM = ('Example market report', 'https://www.wsj.com/example?mod=rss', 'original-guid',
        '<p>Rates held at 4%.</p><script>hidden()</script>', 'Fri, 02 Jan 2026 12:00:00 GMT')


class RssTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'config').mkdir()
        (self.base / 'config/targets.txt').write_text('tibo\n')
        sources.set_rss_enabled(self.base, 'wsj_markets', True)
        self.source = sources.rss_sources(self.base)[0]

    def collect(self, data=None, *, handler=None, now=NOW):
        handler = handler or (lambda req: httpx.Response(200, content=data, headers={'ETag': 'feed-v1'}))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return rss_worker.process_once(self.base, client, now, force=True)

    def records(self):
        return [json.loads(line) for line in sources.source_path(self.base, 'rss:wsj_markets').read_text().splitlines()]

    def test_parse_preserves_attribution_and_removes_html(self):
        posts, invalid = rss_worker.parse_feed(feed(ITEM), self.source, NOW)
        post = posts[0]
        self.assertEqual(invalid, 0)
        self.assertEqual(post['text'], 'Example market report\n\nRates held at 4%.')
        self.assertEqual(post['source_label'], '华尔街日报 · 市场')
        self.assertEqual(post['content_kind'], 'feed_summary')
        self.assertEqual(post_cleaner.source_post(post)['url'], ITEM[1])
        self.assertTrue(post['id'].startswith('rss:'))

    def test_atom_and_missing_dates(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Example</title>
        <entry><id>one</id><title>Policy statement</title><link href="https://example.com/one"/>
        <updated>2026-01-02T12:00:00Z</updated><summary>Rates unchanged.</summary></entry>
        <entry><id>two</id><title>No date</title><link href="https://example.com/two"/></entry></feed>'''
        posts, invalid = rss_worker.parse_feed(atom, self.source, NOW)
        self.assertEqual(len(posts), 1)
        self.assertEqual(invalid, 1)
        with self.assertRaises(ValueError):
            rss_worker.parse_feed(b'<html>upstream error</html>', self.source, NOW)

    def test_repeated_headlines_with_different_article_urls_remain_distinct(self):
        second = (ITEM[0], 'https://www.wsj.com/different-event', 'two', *ITEM[3:])
        posts, _ = rss_worker.parse_feed(feed(ITEM, second), self.source, NOW)
        self.assertNotEqual(post_cleaner.clean_post(posts[0])['duplicate_group'],
                            post_cleaner.clean_post(posts[1])['duplicate_group'])

    def test_dedup_survives_guid_and_tracking_changes(self):
        self.assertEqual(self.collect(feed(ITEM))['new'], 1)
        changed = (ITEM[0], 'https://www.wsj.com/example?utm_source=feed', 'new-guid', *ITEM[3:])
        self.assertEqual(self.collect(feed(changed))['new'], 0)
        self.assertEqual(len(self.records()), 1)

    def test_initial_cutoff_and_future_entries(self):
        old = ('Old', 'https://www.wsj.com/old', 'old', 'Old content', 'Wed, 31 Dec 2025 12:00:00 GMT')
        future = ('Future', 'https://www.wsj.com/future', 'future', 'Future content', 'Sun, 04 Jan 2026 12:00:00 GMT')
        self.assertEqual(self.collect(feed(ITEM, old, future))['new'], 1)
        state = rss_worker.load_status(self.base)['rss:wsj_markets']
        self.assertIsNone(state['etag'])
        self.assertEqual(self.collect(feed(ITEM, old, future), now=NOW + timedelta(days=2))['new'], 1)

    def test_conditional_request_and_error_keep_existing_content(self):
        self.collect(feed(ITEM))
        def not_modified(req):
            self.assertEqual(req.headers['If-None-Match'], 'feed-v1')
            return httpx.Response(304)
        result = self.collect(handler=not_modified)
        self.assertEqual(result['new'], 0)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertIsNone(rss_worker.load_status(self.base)['rss:wsj_markets']['error'])
        self.assertEqual(self.collect(handler=lambda req: httpx.Response(503))['failed'], 1)
        self.assertEqual(len(self.records()), 1)
        state = rss_worker.load_status(self.base)['rss:wsj_markets']
        self.assertIn('HTTP 503', state['error'])
        self.assertGreater(datetime.fromisoformat(state['next_check_at']), NOW)

    def test_redirect_cannot_reach_private_or_other_hosts(self):
        calls = []
        def redirect(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/secrets'})
        self.assertEqual(self.collect(handler=redirect)['failed'], 1)
        self.assertEqual(len(calls), 1)

    def test_interrupted_append_recovers_and_reuses_saved_ids(self):
        self.collect(feed(ITEM))
        with sources.source_path(self.base, 'rss:wsj_markets').open('ab') as file:
            file.write(b'{"id": "unfinished')
        result = self.collect(feed(ITEM))
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['new'], 0)
        self.assertEqual(len(self.records()), 1)

    def test_enrichment_cleaning_and_daily_snapshot_share_rss_identity(self):
        self.collect(feed(ITEM))
        with patch.object(ai_worker, 'BASE', self.base):
            posts = ai_worker.all_posts()
        self.assertEqual(len(posts), 1)
        cleaned = clean_worker.process_once(self.base, now=NOW)
        self.assertEqual(cleaned['cleaned'], 1)
        snapshot = daily_worker.snapshot_posts('2026-01-02', self.base)
        self.assertEqual(snapshot['posts'][0]['url'], ITEM[1])
        prepared, _ = daily_worker.prepare_posts(snapshot)
        self.assertEqual(prepared[0]['account'], '华尔街日报 · 市场')
        self.assertEqual(prepared[0]['content_kind'], 'RSS 标题与摘要')
        self.assertEqual(prepared[0]['id'], posts[0]['id'])
        sources.set_rss_enabled(self.base, 'wsj_markets', False)
        self.assertEqual(daily_worker.snapshot_posts('2026-01-02', self.base)['posts'], [])

    def test_web_filter_export_and_subscription_toggle(self):
        self.collect(feed(ITEM))
        with patch.multiple(web_app, BASE=self.base, TARGETS=self.base / 'config/targets.txt',
                            STATUS=self.base / 'status.json', ANNOTATIONS=self.base / 'annotations.json',
                            LINK_CACHE=self.base / 'links.json'), \
                patch.dict(web_app.app.config, TESTING=True, SESSION_COOKIE_SECURE=False):
            client = web_app.app.test_client()
            with client.session_transaction() as session:
                session['authenticated'] = True
                session['csrf'] = 'test-csrf'
            response = client.get('/?_partial=1&account=rss:wsj_markets')
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['count'], 1)
            self.assertIn('华尔街日报', data['rows_html'])
            self.assertIn('https://www.wsj.com/example', data['rows_html'])
            self.assertNotIn('https://x.com/rss', data['rows_html'])
            exported = client.get('/targets/rss:wsj_markets/export?format=csv')
            self.assertEqual(exported.status_code, 200)
            self.assertIn('Rates held at 4%', exported.get_data(as_text=True))
            self.assertEqual(client.post('/rss/wsj_markets/toggle', data={'enabled': '0'}).status_code, 400)
            self.assertEqual(client.post('/rss/wsj_markets/toggle', data={'enabled': '0', 'csrf_token': 'test-csrf'}).status_code, 302)
            self.assertEqual(sources.rss_sources(self.base), [])


if __name__ == '__main__':
    unittest.main()
