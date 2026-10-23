# 部署指南

以下以 Linux + systemd、项目目录 `/srv/x-monitor` 为例，使用 Python 3.10+。

## 安装与采集

```bash
cd /srv
git clone https://github.com/mmQuo/x-monitor.git
cd x-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/targets.example.txt config/targets.txt
```

在 `config/targets.txt` 中填写要监控的用户名，每行一个。导入包含 `auth_token` 与 `ct0` 的 X 账号 Cookie，然后运行一次采集：

```bash
.venv/bin/twscrape --db accounts.db add_cookie my_account
chmod 600 accounts.db
TWS_TELEMETRY=0 .venv/bin/python monitor.py --targets-file config/targets.txt --once
```

持续采集可使用 `--interval 300`，或按下方步骤安装 systemd 服务。

## 目录与服务用户

仓库中的服务单元使用 `xmonitorweb` 提供网页，`xmonitorai` 运行中文整理、预清洗、短链与日报。两者共享 `xmonitorweb` 组。采集服务沿用系统服务默认用户 root，Cookie 数据库保持 `0600`，由采集进程使用。

在初次部署时以 root 执行：

```bash
cd /srv/x-monitor
groupadd --system xmonitorweb
useradd --system --gid xmonitorweb --no-create-home --shell /usr/sbin/nologin xmonitorweb
useradd --system --gid xmonitorweb --no-create-home --shell /usr/sbin/nologin xmonitorai
chgrp xmonitorweb /srv/x-monitor
chmod 2750 /srv/x-monitor
install -d -o xmonitorweb -g xmonitorweb -m 2770 config
chown xmonitorweb:xmonitorweb config/targets.txt
chmod 660 config/targets.txt
install -d -o xmonitorai -g xmonitorweb -m 2750 ai_data link_data
install -d -o xmonitorai -g xmonitorweb -m 2770 clean_data report_data
```

根目录的 setgid 位使新建原帖和状态文件继承共享组。若已运行过采集，先将已有的 `*_posts.jsonl`、`*.baseline_at` 和 `monitor_status.json` 设为 `xmonitorweb` 组可读。`accounts.db` 保持 `0600` 权限。源码、静态资源及虚拟环境为两个服务用户提供读取 / 执行权限。已存在的用户和目录沿用即可。

## 环境变量

示例文件为 [`deploy/web.env.example`](../deploy/web.env.example) 与 [`deploy/ai.env.example`](../deploy/ai.env.example)。初次部署时复制到仓库外，替换全部占位符：

```bash
install -o root -g xmonitorweb -m 640 deploy/web.env.example /etc/x-monitor-web.env
install -o root -g xmonitorweb -m 640 deploy/ai.env.example /etc/x-monitor-ai.env
```

| 变量 | 用途 |
| --- | --- |
| `XMONITOR_WEB_SECRET` | 随机会话签名密钥；更换后已有登录失效 |
| `XMONITOR_WEB_USERNAME` | 网页登录用户名 |
| `XMONITOR_WEB_PASSWORD_HASH` | Werkzeug 生成的登录密码哈希 |
| `XMONITOR_AI_BASE_URL` | 兼容接口的基础地址，通常包含 `/v1`；应用追加 `/chat/completions` |
| `XMONITOR_AI_API_KEY` | 模型服务密钥 |
| `XMONITOR_AI_MODEL` | 网关支持的模型 ID；摘要和日报共享此配置 |
| `XMONITOR_REPORT_DIR` | 可选，默认项目下 `report_data/`；改变后需同时调整网页 / 日报环境与服务写入目录 |

在服务器终端生成会话密钥和密码哈希，再填入网页环境文件：

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
.venv/bin/python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass("Web password: ")))'
```

环境文件采用每行 `KEY=value` 的格式，由 systemd 加载。

## 安装 systemd 服务

```bash
cp deploy/systemd/x-monitor-*.service /etc/systemd/system/
cp deploy/systemd/x-monitor-links.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now x-monitor-monitor.service x-monitor-ai.service x-monitor-web.service
systemctl enable --now x-monitor-clean.service x-monitor-daily.service x-monitor-links.timer
```

| 单元 | 职责 / 周期 | 可写目录 |
| --- | --- | --- |
| `x-monitor-monitor.service` | 默认每 300 秒检查账号 | 根目录原帖、基线和状态、会话数据库 |
| `x-monitor-ai.service` | 每 5 秒处理一批，最多 10 条 | `ai_data/` |
| `x-monitor-web.service` | Gunicorn，2 个进程、每个 16 个线程 | `config/`、`report_data/` |
| `x-monitor-links.timer` | 启动后 3 分钟、以后每 10 分钟运行短链服务 | `link_data/` |
| `x-monitor-clean.service` | 每 30 秒处理今天和昨天 | `clean_data/` |
| `x-monitor-daily.service` | 北京时间 08:00 为前一天排队，持续处理网页任务 | `report_data/`、`clean_data/` |

网页和 AI 服务启用 `ProtectSystem=strict`，按表中的目录配置写入权限。日报 / 预清洗使用共享组及 `UMask=0007`，保证 SQLite 和 WAL 可由相应进程访问。更换部署路径时同步修改每个单元的 `WorkingDirectory`、`ExecStart` 和 `ReadWritePaths`。

## HTTPS 与实时更新

网页监听 `127.0.0.1:8765`，通过 HTTPS 反向代理访问。会话 Cookie 启用 `Secure`，在 HTTPS 连接中保持登录。

例如在已有 TLS 配置的 Nginx `server` 中加入：

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 660s;
}
```

`/events` 和 `/reports/stream` 使用 SSE；代理需允许长连接并关闭缓冲。资讯页在连接中断时每 30 秒尝试刷新，X 采集周期仍由后台服务决定。日报时间固定为北京时间；总览的日期、发文统计与时间显示跟随服务器时区，建议部署主机使用 `Asia/Shanghai`。

## 常用操作

查看服务与日志：

```bash
systemctl status x-monitor-monitor.service x-monitor-web.service x-monitor-daily.service
journalctl -u x-monitor-monitor.service -f
journalctl -u x-monitor-ai.service -f
journalctl -u x-monitor-daily.service -f
```

账号修改在下一轮采集时生效；修改环境变量后重启对应服务。网页账号列表可导出完整历史 CSV / JSONL，日报页面可导出 Markdown。移除账号后，历史文件继续保留。

单次命令应在项目目录、相应服务用户和相同环境下运行；中文整理与日报需先加载 AI 环境变量：

```bash
.venv/bin/python ai_worker.py --once
.venv/bin/python link_backfill.py
.venv/bin/python clean_worker.py --once
.venv/bin/python daily_worker.py --date 2026-01-02 --once
```

后台日报服务已运行时，通过网页提交任务，由运行中的 worker 统一处理。当天日报标注数据截止时间；失败后可继续重试，已完成步骤会复用。

采集支持重复传入 `--username`，也支持 `--output-dir`。整套工作台默认读取项目根目录中的 JSONL；调整输出目录时，同步配置各模块的数据位置。需要重新建立某账号基线时，先停止采集并备份该账号 JSONL 与对应 `.baseline_at`，移走后再启动。

升级前备份 `config/targets.txt`、会话文件、原始帖子、AI / 链接数据以及清洗和日报数据库。SQLite 使用 backup API 或停服务后复制，完整保存主文件与 WAL 中的数据。服务单元有改动时重新安装并执行 `systemctl daemon-reload`，随后重启涉及的服务。
