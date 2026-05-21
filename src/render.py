"""Render mailbox-friendly weekly report HTML."""

from __future__ import annotations

import html
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "weekly_email.html"


def render_weekly_email(
    repositories: Sequence[Mapping[str, Any]],
    *,
    week_start: date | str,
    week_end: date | str,
    generated_at: datetime | str | None = None,
    template_path: str | Path = DEFAULT_TEMPLATE_PATH,
    config: Any | None = None,
) -> str:
    """Render the weekly email HTML.

    Jinja2 is used when available. If the dependency is not installed yet, a
    small built-in renderer keeps local smoke tests usable.
    """

    context = build_email_context(
        repositories,
        week_start=week_start,
        week_end=week_end,
        generated_at=generated_at,
    )

    resolved_template_path = Path(
        _config_value(config, "email_template_path", default=template_path)
        or template_path
    )

    try:
        return _render_with_jinja(context, resolved_template_path)
    except ImportError:
        LOGGER.warning("jinja2 package is not installed; using built-in renderer")
        return _render_without_jinja(context)


def build_email_context(
    repositories: Sequence[Mapping[str, Any]],
    *,
    week_start: date | str,
    week_end: date | str,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    normalized_repositories = [_normalize_repository(repository, index + 1) for index, repository in enumerate(repositories)]
    generated = _normalize_generated_at(generated_at)
    return {
        "title": "GitHub 本周 Star 增长最快 Top 10",
        "week_start": _format_date(week_start),
        "week_end": _format_date(week_end),
        "generated_at": generated,
        "metric_note": "基于 GH Archive WatchEvent 统计，代表新增 Star 事件数；取消 Star 不会扣除。",
        "repositories": normalized_repositories,
    }


def _render_with_jinja(context: Mapping[str, Any], template_path: Path) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)
    return template.render(**context)


def _render_without_jinja(context: Mapping[str, Any]) -> str:
    rows = "\n".join(_table_row(repository) for repository in context["repositories"])
    details = "\n".join(_detail_block(repository) for repository in context["repositories"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(context["title"])}</title>
</head>
<body>
  <h1>{html.escape(context["title"])}</h1>
  <p>统计周期：{html.escape(context["week_start"])} 至 {html.escape(context["week_end"])}</p>
  <p>{html.escape(context["metric_note"])}</p>
  <table border="1" cellpadding="6" cellspacing="0">
    <thead>
      <tr>
        <th>排名</th>
        <th>项目</th>
        <th>本周新增 Star</th>
        <th>当前总 Star</th>
        <th>语言</th>
        <th>历史上榜次数</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  {details}
  <p>数据源：GH Archive、GitHub API。生成时间：{html.escape(context["generated_at"])}。</p>
</body>
</html>"""


def _table_row(repository: Mapping[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{repository['rank']}</td>"
        f"<td><a href=\"{html.escape(repository['html_url'])}\">{html.escape(repository['full_name'])}</a></td>"
        f"<td>{repository['stars_gained_text']}</td>"
        f"<td>{repository['total_stars_text']}</td>"
        f"<td>{html.escape(repository['language'])}</td>"
        f"<td>{html.escape(repository['appearance_text'])}</td>"
        "</tr>"
    )


def _detail_block(repository: Mapping[str, Any]) -> str:
    return (
        f"<h2>#{repository['rank']} {html.escape(repository['full_name'])}</h2>"
        f"<p>{html.escape(repository['summary_zh'])}</p>"
    )


def _normalize_repository(repository: Mapping[str, Any], default_rank: int) -> dict[str, Any]:
    rank = _as_int(repository.get("rank"), default_rank)
    total_stars = repository.get("total_stars", repository.get("stargazers_count"))
    appearance_count = _as_int(repository.get("appearance_count") or repository.get("history_count"), 1)

    return {
        "rank": rank,
        "full_name": _clean_text(repository.get("full_name")) or "未知项目",
        "html_url": _safe_url(repository.get("html_url")),
        "stars_gained": _as_int(repository.get("stars_gained"), 0),
        "stars_gained_text": _format_count(repository.get("stars_gained")),
        "total_stars": _as_int(total_stars, 0),
        "total_stars_text": _format_count(total_stars),
        "language": _clean_text(repository.get("language")) or "未标明",
        "description": _clean_text(repository.get("description")),
        "summary_zh": _clean_text(repository.get("summary_zh")) or "项目信息有限，暂未生成中文介绍。",
        "appearance_count": appearance_count,
        "appearance_text": "首次上榜" if appearance_count <= 1 else f"历史第 {appearance_count} 次上榜",
    }


def _format_date(value: date | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _normalize_generated_at(value: datetime | str | None) -> str:
    if value is None:
        value = datetime.now(UTC)
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _safe_url(value: Any) -> str:
    url = _clean_text(value)
    if url.startswith(("https://github.com/", "http://github.com/")):
        return url
    return "https://github.com"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split())


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "未知"


def _config_value(config: Any | None, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        value = config.get(name, default)
        return default if value is None else value
    value = getattr(config, name, default)
    return default if value is None else value


if __name__ == "__main__":
    sample_repositories = [
        {
            "rank": 1,
            "full_name": "example/project",
            "html_url": "https://github.com/example/project",
            "stars_gained": 1234,
            "total_stars": 5678,
            "language": "Python",
            "appearance_count": 2,
            "summary_zh": "这是一个示例项目，用于验证邮件模板渲染。",
        }
    ]
    print(render_weekly_email(sample_repositories, week_start="2026-05-11", week_end="2026-05-17"))
