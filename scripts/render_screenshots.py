"""Capture the dashboard, event map and daily brief using saved data and Chromium."""

import argparse
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]


def copy_display_data(source, destination):
    paths = [source / "config/targets.txt", *source.glob("*_posts.jsonl"),
             *(source / "rss_data").glob("*_posts.jsonl")]
    paths += [source / name for name in (
        "monitor_status.json", "ai_data/annotations.json", "link_data/links.json",
        "config/rss_sources.json", "rss_data/status.json",
        "geo_data/locations.json", "geo_data/status.json",
    ) if (source / name).exists()]
    for path in paths:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    target_db = destination / "report_data/reports.sqlite3"
    target_db.parent.mkdir(parents=True)
    source_db = source / "report_data/reports.sqlite3"
    # Online backup includes committed WAL records without changing the source.
    with sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target_db) as snapshot:
            original.backup(snapshot)
    return target_db


def report_day(database, requested):
    if requested:
        date.fromisoformat(requested)
    with sqlite3.connect(database) as connection:
        if requested:
            row = connection.execute(
                "SELECT day FROM runs WHERE status='complete' AND day=? LIMIT 1",
                (requested,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT day FROM runs WHERE status='complete' ORDER BY day DESC, id DESC LIMIT 1"
            ).fetchone()
    if not row:
        raise ValueError("The source must contain a completed daily report for the requested date")
    return row[0]


def preview_app(snapshot, password):
    os.environ.update(
        XMONITOR_WEB_SECRET=secrets.token_hex(32),
        XMONITOR_WEB_USERNAME="preview",
        XMONITOR_WEB_PASSWORD_HASH=generate_password_hash(password),
        XMONITOR_REPORT_DIR=str(snapshot / "report_data"),
        TZ="Asia/Shanghai",
    )
    time.tzset()
    sys.path.insert(0, str(ROOT))
    import web_app
    import map_web

    web_app.BASE = snapshot
    map_web.BASE = snapshot
    web_app.TARGETS = snapshot / "config/targets.txt"
    web_app.LOCK = snapshot / "config/.targets.lock"
    web_app.STATUS = snapshot / "monitor_status.json"
    web_app.ANNOTATIONS = snapshot / "ai_data/annotations.json"
    web_app.LINK_CACHE = snapshot / "link_data/links.json"
    # The preview binds to loopback and has fresh, per-run credentials.
    web_app.app.config.update(SESSION_COOKIE_SECURE=False)
    return web_app.app


def render(origin, password, day, output, executable):
    from playwright.sync_api import sync_playwright

    routes = [
        ("dashboard-4x3.png", "/", "#post-rows tr[data-post-id]", "#live-state"),
        ("map-4x3.png", "/map", ".map-event", "#map-live"),
        ("daily-4x3.png", f"/reports?date={day}", ".report-event", None),
    ]
    manifest = {
        "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "source": "Saved X posts, RSS articles, AI annotations, event locations and daily reports",
        "report_date": day,
        "screenshots": [],
    }
    errors = []
    with sync_playwright() as playwright:
        options = {"headless": True, "args": ["--enable-unsafe-swiftshader"]}
        if executable:
            options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**options)
        try:
            manifest["browser"] = "Chromium " + browser.version
            context = browser.new_context(
                viewport={"width": 1024, "height": 768}, device_scale_factor=1,
                locale="zh-CN", timezone_id="Asia/Shanghai", color_scheme="light",
            )
            page = context.new_page()
            # Observe the map's public readiness API while preserving its normal rendering.
            page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
                if (!window.maplibregl) return;
                const Map = maplibregl.Map;
                maplibregl.Map = class extends Map {
                    constructor(options) {
                        super(options);
                        window.screenshotMap = this;
                    }
                };
            }, {once: true});""")
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: errors.append(
                f"HTTP {response.status}: {response.url}"
            ) if response.status >= 400 else None)
            page.goto(origin + "/login", wait_until="domcontentloaded")
            page.locator("#username").fill("preview")
            page.locator("#password").fill(password)
            page.locator(".login-form button").click()
            page.wait_for_url(origin + "/", wait_until="domcontentloaded")
            for name, route, selector, live_state in routes:
                page.goto(origin + route, wait_until="domcontentloaded")
                page.locator(selector).first.wait_for()
                if live_state:
                    page.locator(live_state + ".connected").wait_for()
                if route == "/map":
                    page.locator(".map-marker").first.wait_for(timeout=60000)
                    page.wait_for_function(
                        """() => window.screenshotMap?.loaded()
                            && window.screenshotMap.areTilesLoaded()
                            && !window.screenshotMap.isMoving()
                            && document.querySelector('#map-error').hidden""",
                        timeout=60000,
                    )
                page.evaluate("document.fonts.ready")
                page.evaluate("scrollTo(0, 0)")
                dimensions = page.evaluate("""() => ({
                    width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                    visible_rows: [...document.querySelectorAll('#post-rows tr[data-post-id]')]
                        .filter(row => {const r = row.getBoundingClientRect();
                            return r.top >= 0 && r.bottom <= innerHeight;}).length
                })""")
                if dimensions["scrollWidth"] > 1024:
                    raise RuntimeError(f"Horizontal overflow in {name}: {dimensions}")
                page.screenshot(path=str(output / name), animations="disabled")
                item = {"file": name, "route": route, "viewport": {"width": 1024, "height": 768},
                        "rendered_items": page.locator(selector).count()}
                if route == "/":
                    item["fully_visible_feed_rows"] = dimensions["visible_rows"]
                elif route == "/map":
                    item["visible_markers"] = page.locator(".map-marker").count()
                manifest["screenshots"].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
        finally:
            browser.close()
    if errors:
        raise RuntimeError("Browser errors: " + "; ".join(errors))
    (output / "screenshots.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing deployment data directory")
    parser.add_argument("--output", type=Path, default=ROOT / "docs")
    parser.add_argument("--report-date", help="Completed report date; defaults to latest completed day")
    parser.add_argument("--chromium", type=Path, help="Optional installed Chromium executable")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix="x-monitor-screenshots-") as directory:
        snapshot = Path(directory)
        database = copy_display_data(source, snapshot)
        day = report_day(database, args.report_date)
        password = secrets.token_urlsafe(24)
        server = make_server("127.0.0.1", 0, preview_app(snapshot, password), threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            render(f"http://127.0.0.1:{server.server_port}", password, day, output, args.chromium)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
