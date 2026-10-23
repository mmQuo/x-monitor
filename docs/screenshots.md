# 更新页面截图

页面预览展示资讯总览和每日简报，使用 1024 × 768 浏览器视口，由页面自动适配布局。

| 页面 | 路由 | 视口 | 图片 |
| --- | --- | --- | --- |
| 资讯总览 | `/` | 1024 × 768 | [资讯总览](dashboard-desktop.png) |
| 每日简报 | `/reports?date=2026-10-22` | 1024 × 768 | [每日简报](daily-desktop.png) |

## 生成截图

安装 Playwright 和 Chromium：

```bash
python3 -m venv .venv-tools
.venv-tools/bin/pip install -r requirements-screenshots.txt
.venv-tools/bin/python -m playwright install --with-deps chromium
```

Linux 中文字体可使用 Debian / Ubuntu 的 `fonts-noto-cjk`。

从部署目录读取数据并生成两张图片：

```bash
.venv-tools/bin/python scripts/render_screenshots.py --source /srv/x-monitor --output docs
```

`--source` 指向包含账号列表、原帖、AI 注解和已完成日报的目录。脚本默认选取最新已完成日报，可用 `--report-date 2026-10-22` 指定日期，或用 `--chromium /path/to/chromium` 指定浏览器。

生成结果包含两张 PNG 和 [screenshots.json](screenshots.json)，清单记录生成时间、浏览器版本、页面尺寸和显示条数。
