"""Generate Chinese repository summaries with a deterministic fallback."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
README_LIMIT = 6000
SUMMARY_MIN_CHARS = 40
SUMMARY_MAX_CHARS = 500


@dataclass(frozen=True)
class SummaryResult:
    """Result of a repository summary attempt."""

    full_name: str
    summary_zh: str
    used_fallback: bool
    error: str | None = None


def summarize_repositories(
    repositories: Sequence[Mapping[str, Any]],
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    config: Any | None = None,
) -> list[dict[str, Any]]:
    """Return repositories with a ``summary_zh`` field added.

    Each repository is handled independently. A failing OpenAI call for one
    repository falls back to a local Chinese summary and does not interrupt the
    rest of the report.
    """

    return [
        {
            **dict(repository),
            "summary_zh": summarize_repository(
                repository,
                api_key=api_key or _config_value(config, "openai_api_key"),
                model=model or _config_value(config, "openai_model"),
                timeout_seconds=float(
                    _config_value(config, "openai_timeout_seconds", default=timeout_seconds)
                ),
            ).summary_zh,
        }
        for repository in repositories
    ]


def summarize_repository(
    repository: Mapping[str, Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    config: Any | None = None,
) -> SummaryResult:
    """Generate one Chinese repository summary.

    The OpenAI API is optional at runtime. If the key, dependency, network, or
    model response is unavailable, the function returns a safe fallback summary.
    """

    full_name = _clean_text(repository.get("full_name")) or "未知项目"
    resolved_api_key = api_key or _config_value(config, "openai_api_key") or os.getenv("OPENAI_API_KEY")
    resolved_model = model or _config_value(config, "openai_model") or DEFAULT_MODEL
    resolved_timeout = float(_config_value(config, "openai_timeout_seconds", default=timeout_seconds))

    if not resolved_api_key:
        return SummaryResult(full_name, build_fallback_summary(repository), True, "missing OpenAI API key")

    try:
        summary = _call_openai(
            repository,
            api_key=resolved_api_key,
            model=resolved_model,
            timeout_seconds=resolved_timeout,
        )
        return SummaryResult(full_name, summary, False)
    except Exception as exc:  # noqa: BLE001 - external SDKs raise many exception types.
        LOGGER.warning("OpenAI summary failed for %s: %s", full_name, exc)
        return SummaryResult(full_name, build_fallback_summary(repository), True, str(exc))


def build_fallback_summary(repository: Mapping[str, Any]) -> str:
    """Build a deterministic Chinese summary from trusted local fields."""

    full_name = _clean_text(repository.get("full_name")) or "这个项目"
    description = _clean_text(repository.get("description")) or "项目信息有限，仓库描述暂缺"
    language = _clean_text(repository.get("language")) or "未标明主要语言"
    topics = _normalize_topics(repository.get("topics"))
    topics_text = "、".join(topics[:5]) if topics else "暂无明确主题标签"
    stars_gained = _format_count(repository.get("stars_gained"))
    total_stars = _format_count(repository.get("total_stars") or repository.get("stargazers_count"))

    return (
        f"{full_name} 是一个以 {language} 为主要语言的 GitHub 项目。"
        f"仓库描述为：{description}。"
        f"本周新增 Star 约 {stars_gained}，当前总 Star 约 {total_stars}，主题包括 {topics_text}。"
        "由于自动摘要服务暂不可用，以上介绍仅基于仓库元数据生成；建议读者打开项目 README 进一步确认功能边界、维护状态和适用场景。"
    )


def _call_openai(
    repository: Mapping[str, Any],
    *,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc

    client = OpenAI(api_key=api_key, timeout=timeout_seconds)
    prompt = _build_prompt(repository)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是一名谨慎的技术趋势分析师。你只根据用户提供的仓库字段写作，"
                    "信息不足时要明确说明，不编造事实。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=420,
    )

    content = response.choices[0].message.content if response.choices else None
    summary = _clean_summary(content)
    if not summary:
        raise RuntimeError("OpenAI returned an empty summary")
    return summary


def _build_prompt(repository: Mapping[str, Any]) -> str:
    topics = _normalize_topics(repository.get("topics"))
    readme = _clean_text(repository.get("readme") or repository.get("readme_text"))
    if readme:
        readme = readme[:README_LIMIT]

    return "\n".join(
        [
            "请根据下面的 GitHub 仓库信息，用中文介绍这个项目。",
            "要求：",
            "1. 不要夸大，不要编造 README 或字段中没有的信息。",
            "2. 说明项目解决什么问题。",
            "3. 说明可能为什么本周增长快。",
            "4. 说明适合哪些开发者关注。",
            "5. 如果信息不足，请明确说“项目信息有限”。",
            "6. 控制在 180-250 字。",
            "",
            f"仓库名：{_clean_text(repository.get('full_name'))}",
            f"GitHub 描述：{_clean_text(repository.get('description'))}",
            f"主要语言：{_clean_text(repository.get('language'))}",
            f"主题标签：{', '.join(topics) if topics else '无'}",
            f"本周新增 Star：{_format_count(repository.get('stars_gained'))}",
            f"当前总 Star：{_format_count(repository.get('total_stars') or repository.get('stargazers_count'))}",
            f"README 片段：{readme or '无'}",
        ]
    )


def _clean_summary(content: str | None) -> str:
    summary = _clean_text(content)
    if not summary:
        return ""
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "..."
    if len(summary) < SUMMARY_MIN_CHARS:
        return ""
    return summary


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_topics(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，\s]+", value)
    elif isinstance(value, Sequence):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return [part.strip() for part in parts if part and part.strip()]


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
    sample = {
        "full_name": "example/project",
        "description": "An example repository",
        "language": "Python",
        "topics": ["automation", "github"],
        "stars_gained": 1234,
        "total_stars": 5678,
    }
    print(summarize_repository(sample).summary_zh)
