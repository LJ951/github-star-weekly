# GitHub Star Weekly

每周一自动统计全 GitHub 上一个完整 UTC 自然周新增 Star Top 10，生成中文介绍，记录历史上榜次数，并通过邮件发送周报。

第一版优先保证稳定性、幂等性、错误处理和密钥安全。统计数据来自 GH Archive 在 BigQuery 中的 `WatchEvent`，它代表新增 Star 事件数，不扣除取消 Star。

## 功能范围

- 统计上一个完整 UTC 自然周的 GitHub 新增 Star Top 10。
- 使用 GitHub API 补充仓库详情。
- 使用 DeepSeek/OpenAI 兼容 API 生成中文项目介绍，并保留失败 fallback。
- 使用 SQLite 保存历史榜单和发送记录。
- 使用 Resend 发送 HTML 邮件。
- 使用 GitHub Actions 每周一中国时间 09:00 自动运行。

## 本地准备

建议使用 Python 3.11 或更高版本。

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS 或 Linux:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 环境变量

所有配置都从环境变量读取。不要把真实密钥写进代码、README、测试快照或提交历史。

必填:

| 变量 | 用途 |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` 或 `GOOGLE_APPLICATION_CREDENTIALS` | BigQuery Service Account JSON 全文，或凭证 JSON 文件路径 |
| `GITHUB_TOKEN` | 读取公开仓库详情，降低 GitHub API 限流风险 |
| `DEEPSEEK_API_KEY` 或 `AI_API_KEY` 或 `OPENAI_API_KEY` | 生成中文项目介绍 |
| `RESEND_API_KEY` | 发送邮件 |
| `EMAIL_FROM` | 发件邮箱 |
| `EMAIL_TO` | 收件邮箱，多个地址用英文逗号分隔 |

可选:

| 变量 | 默认值 | 用途 |
|---|---|---|
| `APP_ENV` | `local` | 运行环境标识 |
| `DATABASE_PATH` | `data/rankings.sqlite` | SQLite 数据库路径 |
| `AI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 地址 |
| `AI_MODEL` | `deepseek-v4-flash` | 摘要模型 |
| `OPENAI_MODEL` | 空 | 兼容旧配置，优先使用 `AI_MODEL` |
| `GITHUB_API_TIMEOUT_SECONDS` | `20` | GitHub API 超时 |
| `OPENAI_TIMEOUT_SECONDS` | `45` | OpenAI API 超时 |
| `RESEND_TIMEOUT_SECONDS` | `30` | Resend API 超时 |
| `README_MAX_CHARS` | `6000` | 发送给摘要模型的 README 最大字符数 |
| `TOP_N` | `10` | 榜单数量 |
| `DRY_RUN` | `false` | 为 `true` 时后续主流程可跳过真实发送 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

本地可以创建不提交的 `.env`:

```text
GOOGLE_APPLICATION_CREDENTIALS_JSON={...}
GITHUB_TOKEN=...
DEEPSEEK_API_KEY=...
RESEND_API_KEY=...
EMAIL_FROM=weekly@example.com
EMAIL_TO=you@example.com
```

## GitHub Actions Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 中添加:

| Secret 名称 | 内容 |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | Google Service Account JSON 全文 |
| `GH_TOKEN` | GitHub API Token |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `RESEND_API_KEY` | Resend API Key |
| `EMAIL_FROM` | 发件邮箱 |
| `EMAIL_TO` | 收件邮箱 |

Actions 中建议把 `GH_TOKEN` 映射为程序读取的 `GITHUB_TOKEN`。

## 统计口径

- 运行时间：每周一中国时间 09:00 左右，对应 GitHub Actions cron `0 1 * * 1`。
- 统计窗口：上一个完整 UTC 自然周，即周一 00:00:00 到周日 23:59:59。
- 数据源：BigQuery 公共数据集 `githubarchive.day.*`。
- 指标：`WatchEvent` 数量，近似代表新增 Star 事件。
- 排名：按新增 Star 事件数降序取 Top 10。

第一版使用 UTC 自然周，目的是让查询稳定、便宜、容易验证。如果后续需要严格按中国时间自然周统计，可以在查询层增加跨 UTC 日期边界的时间过滤。

## 运行方式

完整流程:

```bash
python -m src.main
```

测试运行但不发送邮件:

```bash
python -m src.main --dry-run
```

也可以设置环境变量:

```bash
DRY_RUN=true python -m src.main
```

配置模块可单独做本地校验:

```bash
python -m src.config
```

如果缺少必填环境变量，校验会列出缺失项，但不会打印任何密钥值。

## 项目结构

```text
src/
  main.py        # 串联完整流程
  config.py      # 读取和校验环境变量
  collect.py     # 从 BigQuery 查询 GH Archive Top N
  enrich.py      # 调 GitHub API 补充仓库信息
  summarize.py   # 调 DeepSeek/OpenAI 兼容 API 生成中文介绍，失败时 fallback
  db.py          # SQLite 历史榜单和发送记录
  render.py      # 渲染 HTML 邮件
  emailer.py     # 调 Resend 发送邮件
templates/
  weekly_email.html
data/
  .gitkeep       # 保留运行数据目录
.github/
  workflows/
    weekly.yml
tests/
requirements.txt
README.md
```
