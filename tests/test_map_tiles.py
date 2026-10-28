"""Satellite delivery must stay authenticated, bounded and usable during outages."""

import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import httpx

os.environ.setdefault('XMONITOR_WEB_SECRET', 'tile-test-session')
os.environ.setdefault('XMONITOR_WEB_USERNAME', 'test')
os.environ.setdefault('XMONITOR_WEB_PASSWORD_HASH', 'unused')

import map_tiles
import map_web
import web_app

JPEG = b'\xff\xd8\xff' + b'fixture-image'


class SatelliteTileTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.reply = lambda request: httpx.Response(200, content=JPEG, headers={'content-type': 'image/jpeg'})

        def handler(request):
            self.requests.append(request)
            return self.reply(request)

        self.upstream = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.upstream.close)
        self.tiles = map_tiles.SatelliteTiles(self.upstream)
        self.patch = patch.object(map_web, '_satellite', self.tiles)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = web_app.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session['authenticated'] = True

    def test_auth_coordinates_and_fixed_upstream(self):
        self.assertEqual(self.client.get('/map/tiles/satellite/2/1/2.jpg').status_code, 401)
        self.assertFalse(self.requests)
        self.login()
        for path in ['19/0/0', '1/2/0', '1/0/2', '-1/0/0']:
            self.assertEqual(self.client.get('/map/tiles/satellite/' + path + '.jpg').status_code, 404)
        self.assertFalse(self.requests)
        response = self.client.get('/map/tiles/satellite/2/1/2.jpg?url=https://example.invalid')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, JPEG)
        self.assertEqual(str(self.requests[0].url), map_tiles.SATELLITE_URL.format(z=2, x=1, y=2))
        self.assertNotIn('cookie', self.requests[0].headers)
        self.assertNotIn('authorization', self.requests[0].headers)

    def test_private_cache_and_conditional_request(self):
        self.login()
        first = self.client.get('/map/tiles/satellite/1/0/0.jpg')
        self.assertEqual(first.headers['Cache-Control'], 'private, max-age=3600')
        self.assertIn('Cookie', first.headers['Vary'])
        second = self.client.get('/map/tiles/satellite/1/0/0.jpg', headers={'If-None-Match': first.headers['ETag']})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('private', second.headers['Cache-Control'])

    def test_outage_uses_stale_cache_then_rejects_old_images(self):
        self.login()
        self.tiles.get(1, 0, 0)
        tile = self.tiles.cache[(1, 0, 0)]
        self.tiles.cache[(1, 0, 0)] = replace(tile, saved_at=time.monotonic() - map_tiles.CACHE_SECONDS - 1)
        self.reply = lambda request: httpx.Response(503)
        response = self.client.get('/map/tiles/satellite/1/0/0.jpg')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, JPEG)
        self.assertEqual(response.headers['Cache-Control'], 'private, max-age=60')
        self.tiles.cache[(1, 0, 0)] = replace(tile, saved_at=time.monotonic() - map_tiles.STALE_SECONDS - 1)
        self.assertEqual(self.client.get('/map/tiles/satellite/1/0/0.jpg').status_code, 503)

    def test_failure_is_not_cached_and_can_recover(self):
        self.login()
        self.reply = lambda request: httpx.Response(503) if len(self.requests) == 1 else httpx.Response(200, content=JPEG, headers={'content-type': 'image/jpeg'})
        response = self.client.get('/map/tiles/satellite/0/0/0.jpg')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '5')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/map/tiles/satellite/0/0/0.jpg').status_code, 200)

    def test_invalid_or_large_provider_response_is_not_served_as_an_image(self):
        for body, mimetype in [(b'<html>error</html>', 'text/html'), (b'error', 'image/jpeg'),
                              (JPEG + b'x' * map_tiles.MAX_TILE_BYTES, 'image/jpeg')]:
            with self.subTest(mimetype=mimetype, size=len(body)):
                self.reply = lambda request: httpx.Response(200, content=body, headers={'content-type': mimetype})
                with self.assertRaises(map_tiles.TileUnavailable):
                    self.tiles.get(0, 0, 0)
                self.assertFalse(self.tiles.cache)

    def test_duplicate_downloads_coalesce_and_cache_is_bounded(self):
        def delayed(request):
            time.sleep(0.02)
            return httpx.Response(200, content=JPEG, headers={'content-type': 'image/jpeg'})
        self.reply = delayed
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.tiles.get(2, 0, 0), range(4)))
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(tile.data == JPEG for tile, _ in results))
        with patch.object(map_tiles, 'MAX_CACHE_BYTES', len(JPEG) * 2):
            self.tiles.get(2, 1, 0)
            self.tiles.get(2, 2, 0)
        self.assertEqual(len(self.tiles.cache), 2)
        self.assertNotIn((2, 0, 0), self.tiles.cache)
        self.assertEqual(self.tiles.size, len(JPEG) * 2)


if __name__ == '__main__':
    unittest.main()
