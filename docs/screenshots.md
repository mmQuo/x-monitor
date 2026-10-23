# 页面截图

本次截图于 **2026-10-23（北京时间）** 使用 Chromium 渲染。页面来自本仓库代码，内容来自部署实例已保存的公开 X 帖子、AI 注解及日报；账号、摘要、统计和事件均由应用正常读取，没有替换页面文案或构造示例记录。

日报为 2026-10-22 的已完成存档，包含 686 条推文与 10 个重点事件。AI 摘要和日报是应用存档中的整理结果，截图不代表独立事实核验或当前行情。

| 页面 | 路由 | 视口 | 图片 |
| --- | --- | --- | --- |
| 资讯总览 | `/` | 1440 × 1000 | [桌面](dashboard-desktop.png) |
| Web3 链上资金 | `/?category=链上资金` | 1440 × 1000 | [桌面](web3-desktop.png) |
| 宏观与地缘 | `/?category=宏观与地缘` | 1440 × 1000 | [桌面](macro-desktop.png) |
| 每日简报 | `/reports?date=2026-10-22` | 1440 × 1100 | [桌面](daily-desktop.png) |
| 资讯总览 | `/` | 390 × 1000 | [手机](dashboard-mobile.png) |
| 每日简报 | `/reports?date=2026-10-22` | 390 × 1100 | [手机](daily-mobile.png) |

图片展示首屏，页面可以继续滚动。[screenshots.json](screenshots.json) 保存实际生成时间、浏览器版本、视口和首屏完整记录数，便于比较后续布局变化。

## 重新生成

使用单独的工具虚拟环境，不需要改动生产环境依赖：

```bash
python3 -m venv .venv-tools
.venv-tools/bin/pip install -r requirements-screenshots.txt
.venv-tools/bin/python -m playwright install chromium
.venv-tools/bin/python scripts/render_screenshots.py --source /srv/x-monitor
```

Linux 需要安装浏览器运行库和中文字体，例如 Debian / Ubuntu 的 `fonts-noto-cjk`；缺少浏览器依赖时可执行 `.venv-tools/bin/python -m playwright install --with-deps chromium`。中文字体不同会影响字形与断行。

可通过参数指定日期、输出目录和现有 Chromium：

```bash
.venv-tools/bin/python scripts/render_screenshots.py \
  --source /srv/x-monitor \
  --report-date 2026-10-22 \
  --output docs \
  --chromium /path/to/chromium
```

`--source` 应包含 `config/targets.txt`、原帖 JSONL、`ai_data/annotations.json` 及至少一期已完成的 `report_data/reports.sqlite3`。Web3 与宏观分类需各有可显示的保存记录；未指定日期时选取最新已完成日报。脚本不会调用 X 或模型来补齐数据。

脚本将显示数据复制到临时目录，通过 SQLite backup API 复制日报，使用随机凭据启动只绑定 localhost 的预览服务。浏览器完成登录后直接截图，同时检查横向溢出、页面异常和 HTTP 错误；结束时关闭服务并移除临时副本。仅将图片和截图清单写入输出目录。

以后更新数据或界面时，重新执行脚本并同步 README / 本文中的截图日期和日报说明。Cookie、环境文件、原始 JSONL 与数据库不随图片发布。
