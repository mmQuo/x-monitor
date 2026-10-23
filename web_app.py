"""Password protected web dashboard for monitored X accounts."""

import csv
import fcntl
import gzip
import hmac
import io
import json
import os
import re
import secrets
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, stream_with_context, url_for
from werkzeug.security import check_password_hash

from link_utils import link_label, linkify, links_for_post
from report_web import reports
from categories import CATEGORY_FILTERS, MACRO_CATEGORIES, MACRO_GROUP, matches_category
from sources import RSS_FEEDS, article_url, rss_sources, set_rss_enabled, source_label, source_path

BASE = Path(__file__).resolve().parent
TARGETS = BASE / "config" / "targets.txt"
LOCK = BASE / "config" / ".targets.lock"
STATUS = BASE / "monitor_status.json"
ANNOTATIONS = BASE / "ai_data" / "annotations.json"
LINK_CACHE = BASE / "link_data" / "links.json"
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")

app = Flask(__name__)
app.register_blueprint(reports)
app.json.ensure_ascii = False
app.secret_key = os.environ.get("XMONITOR_WEB_SECRET", "")
PASSWORD_HASH = os.environ.get("XMONITOR_WEB_PASSWORD_HASH", "")
USERNAME = os.environ.get("XMONITOR_WEB_USERNAME", "")
if not app.secret_key or not PASSWORD_HASH or not USERNAME:
    raise RuntimeError("XMONITOR_WEB_SECRET, XMONITOR_WEB_USERNAME and XMONITOR_WEB_PASSWORD_HASH must be configured")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=16 * 1024,
)


def read_targets() -> list[str]:
    with TARGETS.open(encoding="utf-8") as file:
        return [line.strip().lstrip("@") for line in file if line.strip() and not line.lstrip().startswith("#")]


def change_targets(operation):
    """Lock, edit and atomically replace the file read by the monitor."""
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = read_targets()
        result = operation(names)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=TARGETS.parent, delete=False) as file:
            file.write("# Monitored X usernames, one per line.\n")
            file.writelines(f"{name}\n" for name in names)
            temp = Path(file.name)
        try:
            os.chmod(temp, 0o660)
            os.replace(temp, TARGETS)
        finally:
            temp.unlink(missing_ok=True)
        return result


def normalize(value: str) -> str:
    name = value.strip().lstrip("@")
    if not HANDLE.fullmatch(name):
        raise ValueError("用户名只能包含 1–15 位英文字母、数字或下划线")
    return name


def output_for(username: str) -> Path:
    return source_path(BASE, username)


def export_account_name(name: str) -> str:
    """Resolve an active account without accepting arbitrary file paths."""
    if name.startswith("rss:") and name[4:] in RSS_FEEDS:
        return name
    try:
        requested = normalize(name)
    except ValueError:
        abort(404)
    matched = next((item for item in read_targets() if item.lower() == requested.lower()), None)
    if matched is None:
        abort(404)
    return matched


def csv_cell(value) -> str:
    """Keep tweet text from becoming a spreadsheet formula when CSV is opened."""
    cell = "" if value is None else str(value)
    if cell.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + cell
    return cell


def csv_export_rows(path: Path, annotations: dict, link_cache: dict):
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def row(values):
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(csv_cell(value) for value in values)
        return buffer.getvalue()

    yield "\ufeff" + row((
        "内容ID", "来源", "发布时间", "保存时间", "原文", "原文链接",
        "分类", "AI摘要", "中文翻译", "外部链接", "初次基线",
    ))
    with path.open(encoding="utf-8") as file:
        for line in file:
            try:
                post = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(post, dict) or "id" not in post:
                continue
            ai = annotations.get(str(post["id"])) or {}
            expanded_links = list(dict.fromkeys(links_for_post(post, link_cache).values()))
            yield row((
                post["id"], source_label(post), post.get("created_at"),
                post.get("observed_at"), post.get("text"), post.get("url"),
                ai.get("category"), ai.get("summary"), ai.get("translation"),
                "\n".join(expanded_links), "是" if post.get("baseline") else "否",
            ))


def read_posts(path: Path, keep: int = 300) -> tuple[int, list[dict], dict[str, int]]:
    total = 0
    recent: deque[dict] = deque(maxlen=keep)
    daily: dict[str, int] = {}
    oldest = (datetime.now().astimezone().date() - timedelta(days=6)).isoformat()
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                try:
                    post = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(post, dict) and "id" in post:
                    total += 1
                    recent.append(post)
                    try:
                        day = datetime.fromisoformat(post["created_at"]).astimezone().date().isoformat()
                    except (KeyError, TypeError, ValueError):
                        continue
                    if day >= oldest:
                        daily[day] = daily.get(day, 0) + 1
    except FileNotFoundError:
        pass
    return total, list(recent), daily


def read_status() -> dict:
    try:
        with STATUS.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


def read_rss_status():
    try:
        return json.loads((BASE / "rss_data/status.json").read_text())
    except (OSError, ValueError):
        return {}


def read_annotations() -> dict:
    try:
        with ANNOTATIONS.open(encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_link_cache() -> dict:
    try:
        data = json.loads(LINK_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def file_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def feed_versions() -> tuple:
    paths = sorted(BASE.glob("*_posts.jsonl")) + sorted((BASE / "rss_data").glob("*_posts.jsonl"))
    posts = tuple((str(path.relative_to(BASE)), file_stamp(path)) for path in paths)
    status = (file_stamp(STATUS), file_stamp(BASE / "rss_data/status.json"))
    targets = (file_stamp(TARGETS), file_stamp(BASE / "config/rss_sources.json"))
    return posts, file_stamp(ANNOTATIONS), status, targets, file_stamp(LINK_CACHE)


def pretty_time(value: str | None) -> str:
    if not value:
        return "尚未检查"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


app.jinja_env.filters["localtime"] = pretty_time


def short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value


app.jinja_env.filters["shorttime"] = short_time
app.jinja_env.filters["linkify"] = linkify
app.jinja_env.filters["link_label"] = link_label


def activity_chart(daily: dict[str, int]) -> list[dict]:
    today = datetime.now().astimezone().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    peak = max([daily.get(day.isoformat(), 0) for day in days] or [0])
    chart = []
    for index, day in enumerate(days):
        count = daily.get(day.isoformat(), 0)
        height = round(count / peak * 61) if peak else 0
        chart.append({"label": day.strftime("%m/%d"), "count": count,
                      "x": 12 + index * 48, "height": height, "y": 72 - height})
    return chart


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            abort(400)


@app.after_request
def secure_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = (
        "no-cache, no-transform" if response.mimetype == "text/event-stream" else "no-store"
    )
    return response


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_ok = hmac.compare_digest(USERNAME, request.form.get("username", ""))
        password_ok = check_password_hash(PASSWORD_HASH, request.form.get("password", ""))
        if username_ok and password_ok:
            session.clear()
            session["authenticated"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("dashboard"))
        flash("密码错误，请重试。", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    names = read_targets()
    rss = {source["name"]: source for source in rss_sources(BASE, include_disabled=True)}
    names += list(rss)
    status = read_status()
    status.update(read_rss_status())
    annotations = read_annotations()
    link_cache = read_link_cache()
    selected = request.args.get("account", "").lower()
    query = request.args.get("q", "").strip().lower()[:100]
    category = request.args.get("category", "")
    categories = CATEGORY_FILTERS
    if category not in categories:
        category = ""
    accounts = []
    posts = []
    daily: dict[str, int] = {}
    for name in names:
        count, recent, account_daily = read_posts(output_for(name))
        current_status = status.get(name.lower(), {})
        metadata = rss.get(name, {"label": "@" + name, "kind": "x", "enabled": True})
        accounts.append({"name": name, "count": count, "status": current_status, **metadata,
                         "today": account_daily.get(datetime.now().astimezone().date().isoformat(), 0)})
        if selected and name.lower() != selected:
            continue
        for day, amount in account_daily.items():
            daily[day] = daily.get(day, 0) + amount
        for post in recent:
            ai = annotations.get(str(post["id"]))
            if not matches_category(category, (ai or {}).get("category")):
                continue
            post["link_map"] = links_for_post(post, link_cache)
            post["visible_links"] = list(post["link_map"].values())[:1]
            haystack = " ".join(str(value) for value in (
                post.get("text", ""), (ai or {}).get("summary", ""),
                (ai or {}).get("translation", ""), *post["link_map"].values()))
            if query and query not in haystack.lower():
                continue
            post["account"] = name
            post["source_display"] = metadata["label"]
            post["article_url"] = article_url(dict(post, username=name))
            post["preview"] = " ".join(str(post.get("text", "")).split())
            post["ai"] = ai
            posts.append(post)
    posts.sort(key=lambda post: (post.get("created_at", ""), str(post.get("id", ""))), reverse=True)
    today = datetime.now().astimezone().date()
    chart = activity_chart(daily)
    last_checked = max((item.get("status", {}).get("checked_at", "") for item in accounts), default="")
    visible_posts = posts[:100]
    if request.args.get("_partial") == "1":
        response = jsonify({
            "rows_html": render_template("_post_rows.html", posts=visible_posts),
            "chart_html": render_template("_activity_chart.html", chart=chart),
            "accounts_html": render_template("_account_rows.html", accounts=accounts),
            "count": len(visible_posts),
            "today": daily.get(today.isoformat(), 0),
            "total_posts": sum(x["count"] for x in accounts),
            "ai_count": len(annotations),
            "last_checked": pretty_time(last_checked),
        })
        response.headers["Vary"] = "Accept-Encoding"
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            response.set_data(gzip.compress(response.get_data(), compresslevel=5))
            response.headers["Content-Encoding"] = "gzip"
        return response
    return render_template(
        "dashboard.html", accounts=accounts, posts=visible_posts, selected=selected,
        query=request.args.get("q", "")[:100], total_posts=sum(x["count"] for x in accounts),
        chart=chart, today_posts=daily.get(today.isoformat(), 0), last_checked=last_checked,
        categories=categories, category=category, ai_count=len(annotations),
        macro_group=MACRO_GROUP, macro_categories=MACRO_CATEGORIES,
    )


@app.get("/events")
@login_required
def events():
    def stream():
        previous = feed_versions()
        started = time.monotonic()
        last_message = time.monotonic()
        yield "retry: 5000\n\n"
        while time.monotonic() - started < 600:
            time.sleep(2)
            current = feed_versions()
            if current != previous:
                change = {
                    "posts": current[0] != previous[0],
                    "ai": current[1] != previous[1],
                    "status": current[2] != previous[2],
                    "targets": current[3] != previous[3],
                    "links": current[4] != previous[4],
                }
                previous = current
                last_message = time.monotonic()
                yield f"event: update\ndata: {json.dumps(change, separators=(',', ':'))}\n\n"
            elif time.monotonic() - last_message >= 15:
                last_message = time.monotonic()
                yield ": keepalive\n\n"

    response = Response(stream(), mimetype="text/event-stream")
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.get("/targets/<name>/export")
@login_required
def export_target(name):
    account = export_account_name(name)
    path = output_for(account)
    if not path.is_file():
        abort(404)
    format_name = request.args.get("format", "csv")
    if format_name == "jsonl":
        return send_file(path, mimetype="application/x-ndjson", as_attachment=True,
                         download_name=f"{account}_posts.jsonl")
    if format_name != "csv":
        abort(400)
    response = Response(
        stream_with_context(csv_export_rows(path, read_annotations(), read_link_cache())),
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{account}_posts.csv"'
    return response


@app.post("/targets")
@login_required
def create_target():
    try:
        name = normalize(request.form.get("username", ""))

        def add(names):
            if any(x.lower() == name.lower() for x in names):
                raise ValueError("该账号已在监控列表中")
            if len(names) >= 100:
                raise ValueError("监控账号数量已达上限 100")
            names.append(name)

        change_targets(add)
        flash(f"已添加 @{name}。下次轮询会建立首次基线。", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("dashboard"))


@app.post("/rss/<feed_id>/toggle")
@login_required
def toggle_rss(feed_id):
    if feed_id not in RSS_FEEDS or request.form.get("enabled") not in {"0", "1"}:
        abort(400)
    enabled = request.form["enabled"] == "1"
    set_rss_enabled(BASE, feed_id, enabled)
    flash(f"{RSS_FEEDS[feed_id]['label']} 已{'启用' if enabled else '暂停'}。", "success")
    return redirect(url_for("dashboard"))


@app.post("/targets/<old_name>/edit")
@login_required
def edit_target(old_name):
    try:
        name = normalize(request.form.get("username", ""))

        def edit(names):
            index = next((i for i, item in enumerate(names) if item.lower() == old_name.lower()), None)
            if index is None:
                raise ValueError("原账号不存在")
            if any(i != index and item.lower() == name.lower() for i, item in enumerate(names)):
                raise ValueError("新账号已在监控列表中")
            names[index] = name

        change_targets(edit)
        flash(f"已将 @{old_name} 更新为 @{name}。", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("dashboard"))


@app.post("/targets/<name>/delete")
@login_required
def delete_target(name):
    try:
        def delete(names):
            if not any(item.lower() == name.lower() for item in names):
                raise ValueError("账号不存在")
            names[:] = [item for item in names if item.lower() != name.lower()]

        change_targets(delete)
        flash(f"已停止监控 @{name}。历史推文文件已保留。", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("dashboard"))


@app.get("/health")
def health():
    return {"status": "ok"}
