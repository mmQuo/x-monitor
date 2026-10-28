"""Read-only event map over saved content and location annotations."""

import hashlib
import gzip
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, session, url_for

from categories import CATEGORY_FILTERS, matches_category
from map_tiles import SatelliteTiles, TileUnavailable
from sources import active_source_names, article_url, rss_sources, source_label, source_path

BASE = Path(__file__).resolve().parent
maps = Blueprint('maps', __name__)
_cache, _lock = {}, threading.Lock()
_satellite = SatelliteTiles()


def authenticated(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapped


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog(base):
    """Cache shared file reads; filters remain per request."""
    names = active_source_names(base)
    paths = [source_path(base, name) for name in names]
    tracked = paths + [base / 'ai_data/annotations.json', base / 'geo_data/locations.json',
        base / 'geo_data/status.json']
    signature = tuple((str(p), (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None) for p in tracked)
    key = str(base)
    with _lock:
        previous = _cache.get(key)
        if previous and previous['signature'] == signature and time.monotonic() - previous['at'] < 30:
            return previous['data']
        annotations = read_json(base / 'ai_data/annotations.json')
        locations = read_json(base / 'geo_data/locations.json')
        now = datetime.now(timezone.utc)
        state = read_json(base / 'geo_data/status.json')
        cutoff = now - timedelta(days=7)
        if state.get('since'):
            cutoff = max(cutoff, datetime.fromisoformat(state['since']))
        posts = []
        for name, path in zip(names, paths):
            if not path.exists():
                continue
            with path.open() as file:
                for line in file:
                    try:
                        post = json.loads(line)
                        stamp = datetime.fromisoformat(post['created_at'])
                        if stamp.tzinfo is None or not cutoff <= stamp <= now:
                            continue
                        key_id = str(post['id'])
                        ai, geo = annotations.get(key_id) or {}, locations.get(key_id) or {}
                        if geo.get('hash') != hashlib.sha256(str(post.get('text') or '').encode()).hexdigest():
                            geo = {}
                        post['username'] = name
                        posts.append({'id': key_id, 'source': name, 'source_label': source_label(post),
                            'url': article_url(post), 'created_at': stamp.isoformat(),
                            'summary': ai.get('summary') or post.get('title') or str(post.get('text') or '')[:240],
                            'category': ai.get('category') or '待分类', 'text': str(post.get('text') or ''),
                            'translation': ai.get('translation') or '', 'locations': geo.get('locations', []),
                            'geo_state': 'failed' if geo.get('error') else 'ready' if geo else 'pending'})
                    except (KeyError, TypeError, ValueError):
                        continue
        data = (posts, state)
        _cache[key] = {'signature': signature, 'at': time.monotonic(), 'data': data}
        return data


@maps.get('/map')
@authenticated
def index():
    rss = {source['name']: source['label'] for source in rss_sources(BASE)}
    sources = [{'name': name, 'label': rss.get(name, '@' + name)} for name in active_source_names(BASE)]
    return render_template('map.html', sources=sources, categories=CATEGORY_FILTERS)


@maps.get('/map/data')
@authenticated
def data():
    period = request.args.get('hours', '24')
    if period not in {'24', '72', '168'}:
        abort(400)
    category = request.args.get('category', '')
    if category and category not in CATEGORY_FILTERS:
        abort(400)
    source, query = request.args.get('source', ''), request.args.get('q', '').strip().lower()[:100]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(period))
    posts, state = catalog(BASE)
    selected = [p for p in posts if datetime.fromisoformat(p['created_at']) >= cutoff
        and (not source or p['source'] == source) and matches_category(category, p['category'])
        and (not query or query in ' '.join((p['text'], p['summary'], p['translation'],
            *(place['name'] for place in p['locations']))).lower())]
    selected.sort(key=lambda p: p['created_at'], reverse=True)
    events, seen = [], {}
    for post in selected:
        if not post['locations']:
            continue
        # Merge only identical source text, never declare different headlines the same event.
        signature = hashlib.sha256(' '.join(post['text'].split()).encode()).hexdigest()
        if signature in seen:
            item = seen[signature]
            item['sources'].append({k: post[k] for k in ('source_label', 'url', 'created_at')})
            continue
        item = {k: post[k] for k in ('id', 'summary', 'category', 'created_at', 'locations', 'text', 'translation')}
        item['sources'] = [{k: post[k] for k in ('source_label', 'url', 'created_at')}]
        seen[signature] = item
        events.append(item)
    response = jsonify(events=events, stats={'total': len(selected), 'located': len(events),
        'unlocated': sum(p['geo_state'] == 'ready' and not p['locations'] for p in selected),
        'pending': sum(p['geo_state'] == 'pending' for p in selected),
        'failed': sum(p['geo_state'] == 'failed' for p in selected)},
        updated_at=state.get('updated_at') or state.get('checked_at'), since=state.get('since'))
    if 'gzip' in request.headers.get('Accept-Encoding', ''):
        response.set_data(gzip.compress(response.get_data(), compresslevel=4))
        response.headers['Content-Encoding'] = 'gzip'
    response.headers['Vary'] = 'Accept-Encoding'
    return response


@maps.get('/map/tiles/satellite/<int:z>/<int:x>/<int:y>.jpg')
def satellite_tile(z, x, y):
    if not session.get('authenticated'):
        abort(401)
    try:
        tile, stale = _satellite.get(z, x, y)
    except ValueError:
        abort(404)
    except TileUnavailable:
        return Response(status=503, headers={'Retry-After': '5'})
    response = Response(tile.data, mimetype=tile.mimetype)
    response.headers['Cache-Control'] = 'private, max-age=' + ('60' if stale else '3600')
    response.headers['Vary'] = 'Cookie'
    response.set_etag(hashlib.sha256(tile.data).hexdigest())
    return response.make_conditional(request)


@maps.get('/map/stream')
@authenticated
def stream():
    base = BASE
    def events():
        previous = None
        started = last_message = time.monotonic()
        yield 'retry: 5000\n\n'
        while time.monotonic() - started < 600:
            paths = [base / path for path in ('geo_data/locations.json', 'ai_data/annotations.json',
                'geo_data/status.json', 'config/targets.txt', 'config/rss_sources.json')]
            signature = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)
            if signature != previous:
                previous = signature
                last_message = time.monotonic()
                yield 'event: update\ndata: {}\n\n'
            elif time.monotonic() - last_message >= 15:
                last_message = time.monotonic()
                yield ': keepalive\n\n'
            time.sleep(2)
    response = Response(events(), mimetype='text/event-stream')
    response.headers['X-Accel-Buffering'] = 'no'
    return response
