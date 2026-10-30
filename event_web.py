"""Authenticated live event ranking, populated by the independent event worker."""
import time
from datetime import datetime, timezone
from flask import Blueprint, Response, jsonify, render_template
from event_store import EventStore
from post_cleaner import digest
from report_web import authenticated

top = Blueprint("top", __name__)


def context():
    store = EventStore(readonly=True)
    now = datetime.now(timezone.utc)
    events, status = store.top(now), store.status(now)
    heartbeat = status.get("heartbeat")
    status["delayed"] = not heartbeat or (now - datetime.fromisoformat(heartbeat)).total_seconds() > 300
    return {"events": events, "processing": status}


@top.get("/top")
@authenticated
def index():
    return render_template("top.html", **context())


@top.get("/top/status")
@authenticated
def status():
    data = context()
    return jsonify({"content": render_template("_top_events.html", **data), "revision": digest(data)})


@top.get("/top/stream")
@authenticated
def stream():
    store = EventStore(readonly=True)
    def events():
        previous = None
        started = last_message = time.monotonic()
        yield "retry: 5000\n\n"
        while time.monotonic() - started < 600:
            # Time alone can expire an event or change its rank.
            signature = (store.meta("heartbeat"), int(time.time() // 60))
            if signature != previous:
                previous = signature
                last_message = time.monotonic()
                yield "event: update\ndata: {}\n\n"
            elif time.monotonic() - last_message >= 15:
                last_message = time.monotonic()
                yield ": keepalive\n\n"
            time.sleep(2)
    response = Response(events(), mimetype="text/event-stream")
    response.headers["X-Accel-Buffering"] = "no"
    return response
