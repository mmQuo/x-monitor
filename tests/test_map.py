"""Verify geographic evidence, incremental processing and authenticated filters."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault('XMONITOR_WEB_SECRET', 'map-test-session')
os.environ.setdefault('XMONITOR_WEB_USERNAME', 'test')
os.environ.setdefault('XMONITOR_WEB_PASSWORD_HASH', 'unused')

import map_places
import map_worker
import map_web
import web_app


class MapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'config').mkdir()
        (self.base / 'config/targets.txt').write_text('tibo\n')
        self.now = datetime.now(timezone.utc)
        self.post = {'id':'123', 'username':'tibo', 'text':'Protests took place in Paris today.',
            'created_at':(self.now - timedelta(hours=2)).isoformat(), 'url':'https://x.com/tibo/status/123'}
        self.write_posts([self.post])

    def write_posts(self, posts):
        (self.base / 'tibo_posts.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in posts))

    def response(self, items):
        return {'items':[{'id':p['id'], 'locations':[{'place_id':'city:paris',
            'relation':'event', 'evidence':'Protests took place in Paris today.'}]} for p in items]}

    def run_worker(self, handler=None):
        def reply(request):
            items = json.loads(json.loads(request.content)['messages'][1]['content'])['items']
            result = self.response(items)
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
        with patch.object(map_worker, 'BASE_URL', 'https://model.invalid/v1'), \
                httpx.Client(transport=httpx.MockTransport(handler or reply)) as client:
            return map_worker.process_once(self.base, client=client, now=self.now)

    def test_candidates_ignore_urls_handles_and_short_pronouns(self):
        self.assertEqual(map_places.candidates('Help us at https://example.com/Paris @London'), [])
        self.assertIn('USA', {p['id'] for p in map_places.candidates('US inflation rises')})
        self.assertIn('city:paris', {p['id'] for p in map_places.candidates('巴黎发生抗议')})

    def test_coordinates_are_local_and_evidence_required(self):
        text = self.post['text']
        allowed = map_places.candidates(text)
        result = map_places.validate_locations([{'place_id':'city:paris','relation':'event',
            'evidence':text, 'coordinates':[0,0]}], text, allowed)
        self.assertEqual(result[0]['coordinates'], [2.352,48.857])
        for item in [{'place_id':'USA','relation':'event','evidence':text},
                     {'place_id':'city:paris','relation':'event','evidence':'Something happened in Paris'},
                     {'place_id':'city:paris','relation':'event','evidence':'today.'}]:
            with self.assertRaises(ValueError): map_places.validate_locations([item], text, allowed)

    def test_country_precision_is_not_a_city(self):
        text='US inflation rose.'
        result=map_places.validate_locations([{'place_id':'USA','relation':'scope','evidence':text}],
            text, map_places.candidates(text))
        self.assertEqual(result[0]['precision'], '国家范围')

    def test_incremental_cache_and_no_location_skip(self):
        self.write_posts([self.post, dict(self.post,id='124',text='New model released today.')])
        state=self.run_worker()
        self.assertEqual(state['located'],1)
        self.assertEqual(state['pending'],0)
        state=self.run_worker(lambda req: self.fail('Cached posts must not call the model'))
        self.assertEqual(state['located'],1)

    def test_failed_model_preserves_prior_annotations(self):
        self.run_worker()
        self.write_posts([self.post,dict(self.post,id='125',text='Paris faces another protest.')])
        state=self.run_worker(lambda req:httpx.Response(503))
        saved=map_worker.load_json(self.base/'geo_data/locations.json')
        self.assertEqual(state['located'],1)
        self.assertTrue(saved['123']['locations'])
        self.assertTrue(saved['125']['error'])
        self.run_worker(lambda req:self.fail('Must respect retry interval'))

    def test_initial_window_does_not_backfill_old_posts(self):
        self.write_posts([self.post,dict(self.post,id='126',created_at=(self.now-timedelta(days=5)).isoformat())])
        state=self.run_worker()
        self.assertEqual(state['total'],1)

    def test_failed_records_stop_after_three_attempts(self):
        for _ in range(3):
            self.run_worker(lambda req:httpx.Response(503))
            self.now += timedelta(hours=1)
        record=map_worker.load_json(self.base/'geo_data/locations.json')['123']
        self.assertEqual(record['attempts'],3)
        self.run_worker(lambda req:self.fail('Failed extraction must have a retry limit'))

    def test_invalid_evidence_does_not_discard_other_items(self):
        second=dict(self.post,id='128',text='Another protest in Paris.')
        self.write_posts([self.post,second])
        state=self.run_worker()
        records=map_worker.load_json(self.base/'geo_data/locations.json')
        self.assertEqual(state['located'],1)
        self.assertTrue(records['123']['locations'])
        self.assertTrue(records['128']['error'])

    def test_map_auth_filters_dedup_and_source_links(self):
        self.write_posts([self.post,dict(self.post,id='127',url='https://x.com/tibo/status/127')])
        self.run_worker()
        with patch.object(map_web,'BASE',self.base), patch.dict(web_app.app.config,TESTING=True,SESSION_COOKIE_SECURE=False):
            client=web_app.app.test_client()
            for path in ['/map','/map/data','/map/stream']:
                self.assertEqual(client.get(path).status_code,302)
            with client.session_transaction() as session: session['authenticated']=True
            response=client.get('/map')
            self.assertEqual(response.status_code,200)
            self.assertIn('事件地图',response.get_data(as_text=True))
            self.assertIn("worker-src 'self'",response.headers['Content-Security-Policy'])
            data=client.get('/map/data').get_json()
            self.assertEqual(len(data['events']),1)
            self.assertEqual(len(data['events'][0]['sources']),2)
            self.assertEqual(data['events'][0]['sources'][0]['url'],self.post['url'])
            self.assertEqual(client.get('/map/data?q=巴黎').get_json()['stats']['located'],1)
            self.assertEqual(client.get('/map/data?source=unknown').get_json()['events'],[])
            self.assertEqual(client.get('/map/data?hours=999').status_code,400)
            self.assertEqual(client.get('/map/data?category=unknown').status_code,400)


if __name__=='__main__': unittest.main()
