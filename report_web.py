"""Authenticated report pages; generation runs in a separate persistent worker."""

import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for

from report_store import ACTIVE, ReportStore, TZ, day_bounds, encode, parse_day, today
from sources import source_label

reports = Blueprint("reports", __name__)
reports.add_app_template_filter(source_label, "source_label")


def authenticated(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapped


def selected_day():
    value = request.args.get("date", (today() - timedelta(days=1)).isoformat())
    try:
        return parse_day(value).isoformat()
    except ValueError:
        abort(400)


@reports.app_template_filter("reporttime")
def report_time(value, format="%m-%d %H:%M"):
    return datetime.fromisoformat(value).astimezone(TZ).strftime(format) if value else "—"


@reports.app_template_filter("reportelapsed")
def report_elapsed(value):
    seconds = max(0, int((datetime.now(TZ) - datetime.fromisoformat(value)).total_seconds()))
    return f"{seconds // 60} 分 {seconds % 60:02d} 秒"


def context(store, day):
    latest = store.latest(day)
    edition = latest if latest and latest["status"] == "complete" else store.latest(day, completed=True)
    source_map = {p["id"]: p for p in edition["snapshot"]["posts"]} if edition else {}
    events = edition["result"]["events"] if edition else []
    # Per-event numbers are generated locally and shared by timeline citations and originals.
    for event in events:
        event["sources"] = [dict(source_map[i], number=n + 1) for n, i in enumerate(event["source_ids"])]
        numbers = {p["id"]: p["number"] for p in event["sources"]}
        for entry in event["timeline"]:
            entry["sources"] = [dict(source_map[i], number=numbers[i]) for i in entry["source_ids"]]
    return {"day": day, "latest": latest, "edition": edition,
            "report": edition["result"] if edition else None, "events": events,
            "partial_edition": bool(edition and datetime.fromisoformat(edition["snapshot"]["captured_at"]) < day_bounds(day)[1]),
            "running": latest is not None and latest["status"] in ACTIVE,
            "history": store.history(), "today": today().isoformat()}


@reports.get("/reports")
@authenticated
def index():
    day = selected_day()
    return render_template("reports.html", **context(ReportStore(), day))


@reports.post("/reports/generate")
@authenticated
def generate():
    day = request.form.get("date", "")
    try:
        parse_day(day)
        ReportStore().enqueue(day)
        flash("日报已加入生成队列，完成后会自动显示。", "success")
    except ValueError as error:
        flash(str(error), "error")
        day = (today() - timedelta(days=1)).isoformat()
    return redirect(url_for("reports.index", date=day))


@reports.get("/reports/status")
@authenticated
def status():
    data = context(ReportStore(), selected_day())
    return jsonify({"content": render_template("_daily_report.html", **data),
                    "history": render_template("_report_history.html", **data),
                    "revision": encode({"updated": (data["latest"] or {}).get("updated_at"),
                                        "history": data["history"]})})


@reports.get("/reports/stream")
@authenticated
def stream():
    day = selected_day()
    store = ReportStore()
    def events():
        previous = None
        started = last_message = time.monotonic()
        yield "retry: 5000\n\n"
        while time.monotonic() - started < 600:
            history = store.history()
            signature = encode(history)
            signature += encode(store.status(day))
            if signature != previous:
                previous = signature
                last_message = time.monotonic()
                yield 'event: update\ndata: {}\n\n'
            elif time.monotonic() - last_message > 15:
                last_message = time.monotonic()
                yield ": keepalive\n\n"
            time.sleep(2)
    response = Response(events(), mimetype="text/event-stream")
    response.headers["X-Accel-Buffering"] = "no"
    return response


def markdown(edition):
    def clean(text):
        # Keep source text from creating links or HTML in Markdown readers.
        for char in "\\`*_{}[]<>#":
            text = text.replace(char, "\\" + char)
        return text.replace("\n", " ")
    result = edition["result"]
    posts = {p["id"]: p for p in edition["snapshot"]["posts"]}
    lines = [f"# {result['day']} 日报", "", f"北京时间 · {result['post_count']} 条内容 · {len(result['events'])} 个重点事件",
             f"数据截至 {report_time(edition['snapshot']['captured_at'])}", ""]
    if not result["events"]:
        lines.append("本期没有可整理的重点事件。")
    for event in result["events"]:
        lines += [f"## {clean(event['title'])}", "", f"{clean(event['category'])} · {clean(event['status'])}", "", clean(event["summary"]), "",
                  "关注点：" + clean(event["reason"]), "", "进展（按来源发布时间）：", ""]
        for entry in event["timeline"]:
            links = " ".join(f"[{clean(source_label(posts[i]))}]({posts[i]['url']})" for i in entry["source_ids"])
            lines.append(f"- {report_time(entry['published_at'], '%H:%M')} {clean(entry['text'])} {links}")
        lines += ["", "来源：" + " · ".join(f"[{clean(source_label(posts[i]))}]({posts[i]['url']})" for i in event["source_ids"])]
        lines.append("")
    lines += [f"生成于 {report_time(result['generated_at'])}（北京时间）", ""]
    return "\n".join(lines)


@reports.get("/reports/<day>/export")
@authenticated
def export(day):
    try:
        parse_day(day)
    except ValueError:
        abort(404)
    edition = ReportStore().latest(day, completed=True)
    if not edition:
        abort(404)
    response = Response(markdown(edition), mimetype="text/markdown; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="x-monitor-daily-{day}.md"'
    return response
