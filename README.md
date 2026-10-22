# X 账号监控台

Poll X accounts with `twscrape`. The first successful check for each account saves a baseline; later checks print new posts and append them to a per-account JSONL file. Reposts are excluded. Existing post IDs are loaded from the JSONL files when the process restarts. The Flask dashboard provides account management, search, AI summaries and translations, live updates, and full CSV/JSONL exports.

## 工作流

![从账号轮询到本地保存、AI 处理、短链接展开、网页查看与导出的工作流](docs/workflow.svg)

网页展示已保存的推文；按账号导出完整 CSV 或 JSONL。

## 页面示意

以下截图以 @tibo、@thsottiaux 等 AI 领域账号名展示虚构演示推文。

### 桌面

![桌面版监控台示意](docs/dashboard-desktop.png)

### 手机

<img src="docs/dashboard-mobile.png" alt="手机版监控台示意" width="390">

## Setup

```bash
cd /srv
git clone https://github.com/mmQuo/x-monitor.git x-monitor
cd /srv/x-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/targets.example.txt config/targets.txt
```

Add cookies from an X account you control. Run this locally; do not paste cookies into a chat. The cookies must include `auth_token` and `ct0`:

```bash
.venv/bin/twscrape --db accounts.db add_cookie my_account
chmod 600 accounts.db
.venv/bin/twscrape --db accounts.db accounts
```

## Run

```bash
TWS_TELEMETRY=0 .venv/bin/python monitor.py --targets-file config/targets.txt --once
TWS_TELEMETRY=0 .venv/bin/python monitor.py --targets-file config/targets.txt --interval 300
```

Add one username per line to `config/targets.txt`, without needing to restart the service. `@` is optional. For example, `tibo` uses `tibo_posts.jsonl`. You can also run a one-time check with repeated flags, such as `--username tibo --username thsottiaux --once`. Use `--output-dir` to choose the directory for account JSONL files.

Each JSONL record includes the post ID, URL, creation time, observation time, text, and a `baseline` flag. The first check fetches up to 100 recent posts from the account timeline; later checks search the latest 20 posts from that account to use a separate rate-limit bucket. Keep `accounts.db` private because it contains session cookies. To start a fresh baseline for an account, move or remove its JSONL file before restarting.

New records also save X's short URL and expanded URL from tweet entities. Historical `t.co` links are cached in `link_data/links.json`; `x-monitor-links.timer` resolves newly found links every 10 minutes, or run `.venv/bin/python link_backfill.py` manually. The backfill requests `t.co` only and reads its redirect target without following the destination. The dashboard links to the expanded URL and falls back to the original `t.co` URL if expansion fails. AI sees expanded URLs but does not treat a destination URL as evidence of the linked page's contents.

The deployment units are in `deploy/systemd/`. They assume this directory is `/srv/x-monitor` and use separate `xmonitorweb` and `xmonitorai` service users. Adjust paths and permissions for another host. The monitor checks every 300 seconds; inspect it with `systemctl status x-monitor-monitor.service` or `journalctl -u x-monitor-monitor.service -f`.

## Web dashboard

The password protected dashboard runs as `x-monitor-web.service` on `127.0.0.1:8765`. Put an HTTPS reverse proxy in front of it; secure cookies require HTTPS. It manages monitored usernames through `config/targets.txt` and displays saved posts and per-account check status. Adding, editing or removing a username takes effect on the next polling cycle. Removing an account leaves its historical JSONL file intact.

The application service stays bound to localhost. Check it with `systemctl status x-monitor-web.service`. Set `XMONITOR_WEB_SECRET`, `XMONITOR_WEB_USERNAME`, and `XMONITOR_WEB_PASSWORD_HASH` in `/etc/x-monitor-web.env`; generate the hash with `werkzeug.security.generate_password_hash`. Set `XMONITOR_AI_BASE_URL`, `XMONITOR_AI_API_KEY`, and optionally `XMONITOR_AI_MODEL` in `/etc/x-monitor-ai.env`. Keep both environment files outside the repository and readable only by the relevant service users.

The web backend uses Flask. The dashboard uses a locally vendored Bootstrap 5 stylesheet, with a small set of layout rules in `static/style.css`.

Each monitored account has CSV and JSONL download links in the account list. Exports include every post saved in that account's JSONL file. CSV includes the original text, tweet URL, timestamps, AI category, summary, translation, and expanded links when available. JSONL downloads the original stored records unchanged.

The authenticated `/events` SSE stream watches saved posts, AI annotations, status, and link cache files. When one changes, the page refreshes the visible feed without navigating away; the X checks themselves still run every 300 seconds.

## 日报

顶部的「日报」页面按北京时间汇总一天的内容，展示重点事件、关键进展和原帖引用。可选择日期手动生成、查看历史、展开原文，或导出 Markdown。当天的日报标注数据截止时间；重新生成后会保存新的版本。生成进度通过 SSE 更新到页面。

下图以虚构的 AI 资讯展示日报布局：

![日报页面示意：重点事件、进展与原帖来源](docs/daily-desktop.png)

`daily_worker.py --schedule` 每天北京时间 08:00 为前一天生成一期日报。启动晚于 08:00 时补上前一天的任务；生成失败后可在页面点击「继续重试」。任务在后台完成，页面关闭后仍会继续运行。

处理流程先固定当前监控账号及当日推文快照，再对重复内容去重、分批提取事件、归并并挑选重点，最后用原帖核对正文。较长的推文会分段提取；最终核对使用所选来源的原文节选。进展时间表示原帖发布时间。每个处理批次都校验引用和推文覆盖范围，全部输入完成处理后才发布一期；日报最多展示 10 个重点事件。

每次模型调用的提示内容最多 24,000 UTF-8 字节，分批材料最多 15,500 字节。总调用量随当日内容增长。中间结果、用量、推文快照和各期日报保存在 `report_data/reports.sqlite3`，中断后会复用已完成的步骤。默认沿用翻译功能的模型配置 `XMONITOR_AI_BASE_URL`、`XMONITOR_AI_API_KEY`、`XMONITOR_AI_MODEL`。

部署前创建可供网页和后台服务共同写入的数据目录，再安装服务文件：

```bash
install -d -o xmonitorai -g xmonitorweb -m 2770 /srv/x-monitor/report_data
cp deploy/systemd/x-monitor-daily.service /etc/systemd/system/
cp deploy/systemd/x-monitor-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now x-monitor-daily.service
systemctl restart x-monitor-web.service
```

服务使用 `UMask=0007`，日报数据目录使用共享组权限。单独运行可用 `.venv/bin/python daily_worker.py --date 2026-01-02 --once`（需先加载 AI 环境变量，并以同一服务用户运行）。后台服务开启时通过网页提交任务，运行日志可通过 `journalctl -u x-monitor-daily.service` 查看。

验证：`.venv/bin/python -m unittest discover -s tests -v`。

## Repository contents

The repository contains source code, templates, static assets, a sample account list, dependencies, and deployment units. Session cookies (`accounts.db`), environment files, actual account lists, saved posts, AI annotations, link caches, daily-report data, and runtime status are excluded by `.gitignore`.
