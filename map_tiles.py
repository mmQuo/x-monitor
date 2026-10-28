"""Fetch satellite tiles from the fixed provider, with a bounded process cache."""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

SATELLITE_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
MAX_TILE_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_CACHE_ITEMS = 512
CACHE_SECONDS = 86400
STALE_SECONDS = 7 * CACHE_SECONDS


class TileUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Tile:
    data: bytes
    mimetype: str
    saved_at: float


class SatelliteTiles:
    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(8, connect=3, pool=2), follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={'User-Agent': 'x-monitor/1.0 (event map)'})
        self.cache = OrderedDict()
        self.size = 0
        self.lock = threading.Lock()
        self.download_locks = [threading.Lock() for _ in range(64)]

    def _cached(self, key):
        with self.lock:
            tile = self.cache.get(key)
            if tile:
                self.cache.move_to_end(key)
            return tile

    def _download(self, z, x, y):
        with self.client.stream('GET', SATELLITE_URL.format(z=z, x=x, y=y)) as response:
            response.raise_for_status()
            mimetype = response.headers.get('content-type', '').split(';')[0].strip().lower()
            if mimetype not in {'image/jpeg', 'image/png'}:
                raise TileUnavailable('Provider did not return an image')
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_TILE_BYTES:
                    raise TileUnavailable('Tile exceeds size limit')
            signature = b'\xff\xd8\xff' if mimetype == 'image/jpeg' else b'\x89PNG\r\n\x1a\n'
            if not data.startswith(signature):
                raise TileUnavailable('Invalid tile content')
            return Tile(bytes(data), mimetype, time.monotonic())

    def get(self, z, x, y):
        if not (0 <= z <= 18 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError('Invalid tile coordinates')
        key = (z, x, y)
        tile = self._cached(key)
        if tile and time.monotonic() - tile.saved_at < CACHE_SECONDS:
            return tile, False
        lock = self.download_locks[hash(key) % len(self.download_locks)]
        if not lock.acquire(timeout=10):
            if tile and time.monotonic() - tile.saved_at < STALE_SECONDS:
                return tile, True
            raise TileUnavailable('Tile download is busy')
        try:
            tile = self._cached(key)
            if tile and time.monotonic() - tile.saved_at < CACHE_SECONDS:
                return tile, False
            try:
                downloaded = self._download(z, x, y)
            except (httpx.HTTPError, TileUnavailable) as exc:
                if tile and time.monotonic() - tile.saved_at < STALE_SECONDS:
                    return tile, True
                raise TileUnavailable('Satellite provider unavailable') from exc
            with self.lock:
                previous = self.cache.pop(key, None)
                self.size -= len(previous.data) if previous else 0
                self.cache[key] = downloaded
                self.size += len(downloaded.data)
                while self.size > MAX_CACHE_BYTES or len(self.cache) > MAX_CACHE_ITEMS:
                    _, removed = self.cache.popitem(last=False)
                    self.size -= len(removed.data)
            return downloaded, False
        finally:
            lock.release()
