"""Enrich collected repositories with GitHub REST API metadata and README text."""

from __future__ import annotations

from collections.abc import Iterator, Mapping as MappingABC
from dataclasses import dataclass, field
import base64
import json
import logging
import os
from typing import Any, Iterable, Mapping, Protocol
from urllib import error, request

LOGGER = logging.getLogger(__name__)

GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_README_CHAR_LIMIT = 6000


class HttpClient(Protocol):
    """Minimal HTTP client interface used for testable GitHub API calls."""

    def get_json(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        ...


@dataclass(frozen=True)
class RepoInput:
    """Repository identity and weekly star count from the collection stage."""

    full_name: str
    stars_gained: int = 0


@dataclass
class EnrichedRepo(MappingABC[str, Any]):
    """GitHub repository metadata needed by downstream summary and email stages."""

    full_name: str
    html_url: str
    stars_gained: int
    total_stars: int | None = None
    language: str | None = None
    description: str | None = None
    topics: list[str] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    pushed_at: str | None = None
    readme_text: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "stars_gained": self.stars_gained,
            "total_stars": self.total_stars,
            "language": self.language,
            "description": self.description,
            "topics": list(self.topics),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pushed_at": self.pushed_at,
            "readme_text": self.readme_text,
            "error": self.error,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


class GitHubApiError(RuntimeError):
    """Raised for GitHub API failures."""

    def __init__(self, status_code: int | None, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


def enrich_repositories(
    repos: Iterable[RepoInput | Mapping[str, Any] | object],
    *,
    token: str | None = None,
    config: Any | None = None,
    http_client: HttpClient | None = None,
    timeout_seconds: float | None = None,
    readme_char_limit: int | None = None,
) -> list[EnrichedRepo]:
    """Enrich multiple repositories.

    A failure for one repository is logged and converted to a fallback
    ``EnrichedRepo`` so the weekly report can still be sent.
    """

    client = http_client or UrlLibHttpClient()
    auth_token = (
        token
        if token is not None
        else _config_value(config, "github_token", "GITHUB_TOKEN")
        or os.getenv("GITHUB_TOKEN")
    )
    resolved_timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else _config_value(
            config,
            "github_api_timeout_seconds",
            "GITHUB_API_TIMEOUT_SECONDS",
            default=DEFAULT_TIMEOUT_SECONDS,
        )
    )
    resolved_readme_limit = int(
        readme_char_limit
        if readme_char_limit is not None
        else _config_value(
            config,
            "readme_max_chars",
            "README_MAX_CHARS",
            default=DEFAULT_README_CHAR_LIMIT,
        )
    )
    enriched: list[EnrichedRepo] = []

    for repo in repos:
        repo_input = coerce_repo_input(repo)
        try:
            enriched.append(
                fetch_repository_details(
                    repo_input,
                    token=auth_token,
                    http_client=client,
                    timeout_seconds=resolved_timeout,
                    readme_char_limit=resolved_readme_limit,
                )
            )
        except Exception as exc:
            safe_message = _safe_error_message(exc, secrets=[auth_token])
            LOGGER.warning(
                "Failed to enrich GitHub repository %s: %s",
                repo_input.full_name,
                safe_message,
            )
            enriched.append(
                fallback_repository(repo_input, error=f"GitHub enrichment failed: {safe_message}")
            )

    return enriched


def fetch_repository_details(
    repo: RepoInput,
    *,
    token: str | None = None,
    http_client: HttpClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    readme_char_limit: int = DEFAULT_README_CHAR_LIMIT,
) -> EnrichedRepo:
    """Fetch GitHub repository metadata and README text for one repository."""

    validate_full_name(repo.full_name)
    client = http_client or UrlLibHttpClient()
    headers = build_headers(token)
    repo_url = f"{GITHUB_API_BASE_URL}/repos/{repo.full_name}"
    payload = client.get_json(repo_url, headers=headers, timeout=timeout_seconds)
    if not isinstance(payload, Mapping):
        raise GitHubApiError(None, "Repository response was not a JSON object")

    try:
        readme_text = fetch_readme_text(
            repo.full_name,
            token=token,
            http_client=client,
            timeout_seconds=timeout_seconds,
            char_limit=readme_char_limit,
        )
    except GitHubApiError as exc:
        LOGGER.warning(
            "Failed to fetch README for %s; continuing with repository metadata only: %s",
            repo.full_name,
            _safe_error_message(exc, secrets=[token]),
        )
        readme_text = None

    return EnrichedRepo(
        full_name=str(payload.get("full_name") or repo.full_name),
        html_url=str(payload.get("html_url") or github_html_url(repo.full_name)),
        stars_gained=repo.stars_gained,
        total_stars=_optional_int(payload.get("stargazers_count")),
        language=_optional_str(payload.get("language")),
        description=_optional_str(payload.get("description")),
        topics=_topics(payload.get("topics")),
        created_at=_optional_str(payload.get("created_at")),
        updated_at=_optional_str(payload.get("updated_at")),
        pushed_at=_optional_str(payload.get("pushed_at")),
        readme_text=readme_text,
    )


def fetch_readme_text(
    full_name: str,
    *,
    token: str | None = None,
    http_client: HttpClient | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    char_limit: int = DEFAULT_README_CHAR_LIMIT,
) -> str | None:
    """Return decoded README text, or ``None`` when README is missing/unusable."""

    validate_full_name(full_name)
    client = http_client or UrlLibHttpClient()
    headers = build_headers(token)
    readme_url = f"{GITHUB_API_BASE_URL}/repos/{full_name}/readme"

    try:
        payload = client.get_json(readme_url, headers=headers, timeout=timeout_seconds)
    except GitHubApiError as exc:
        if exc.status_code == 404:
            return None
        raise

    if not isinstance(payload, Mapping):
        return None

    encoded_content = payload.get("content")
    if not isinstance(encoded_content, str):
        return None
    if payload.get("encoding") and payload.get("encoding") != "base64":
        return None

    try:
        compact_content = "".join(encoded_content.split())
        decoded = base64.b64decode(compact_content, validate=False)
        return decoded.decode("utf-8", errors="replace")[:char_limit]
    except Exception:
        LOGGER.warning("Could not decode README content for %s", full_name)
        return None


def fallback_repository(repo: RepoInput, *, error: str | None = None) -> EnrichedRepo:
    """Build a minimal repository object when GitHub metadata is unavailable."""

    return EnrichedRepo(
        full_name=repo.full_name,
        html_url=github_html_url(repo.full_name),
        stars_gained=repo.stars_gained,
        error=error,
    )


def coerce_repo_input(repo: RepoInput | Mapping[str, Any] | object) -> RepoInput:
    """Accept common row shapes from collect.py without forcing tight coupling."""

    if isinstance(repo, RepoInput):
        return repo
    if isinstance(repo, Mapping):
        full_name = repo.get("full_name")
        stars_gained = repo.get("stars_gained", 0)
    else:
        full_name = getattr(repo, "full_name", None)
        stars_gained = getattr(repo, "stars_gained", 0)

    if not full_name:
        raise ValueError("repository item must contain full_name")
    return RepoInput(full_name=str(full_name), stars_gained=int(stars_gained or 0))


def validate_full_name(full_name: str) -> None:
    """Validate the GitHub owner/repository name used in API URLs."""

    parts = full_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"invalid GitHub repository full_name: {full_name!r}")


def build_headers(token: str | None = None) -> dict[str, str]:
    """Build GitHub API headers without exposing credentials in logs."""

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-star-weekly",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_html_url(full_name: str) -> str:
    return f"https://github.com/{full_name}"


class UrlLibHttpClient:
    """Small urllib-backed JSON client with timeout and GitHub error handling."""

    def get_json(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        req = request.Request(url, headers=dict(headers), method="GET")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            message = _github_error_message(exc)
            raise GitHubApiError(exc.code, message) from exc
        except error.URLError as exc:
            raise GitHubApiError(None, f"Network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitHubApiError(None, "Network timeout") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(None, "Response was not valid JSON") from exc


def _github_error_message(exc: error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:
        payload = {}
    api_message = payload.get("message") if isinstance(payload, Mapping) else None
    if api_message:
        return f"GitHub API HTTP {exc.code}: {api_message}"
    return f"GitHub API HTTP {exc.code}"


def _safe_error_message(exc: Exception, *, secrets: Iterable[str | None] = ()) -> str:
    message = str(exc)
    for secret in [os.getenv("GITHUB_TOKEN"), *secrets]:
        if secret:
            message = message.replace(secret, "***")
    return message


def _topics(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _config_value(config: Any | None, *names: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        for name in names:
            if name in config and config[name] is not None:
                return config[name]
    for name in names:
        if hasattr(config, name):
            value = getattr(config, name)
            if value is not None:
                return value
    return default


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sample = [RepoInput("python/cpython")]
    for item in enrich_repositories(sample):
        print(json.dumps(item.__dict__, ensure_ascii=False, indent=2))
