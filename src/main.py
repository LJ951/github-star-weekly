"""Orchestrate the weekly GitHub Star report.

This module intentionally keeps side effects at the edges:
configuration, external APIs, database writes, and email sending are delegated
to the modules owned by the other implementation threads.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


LOGGER = logging.getLogger("github_star_weekly")
DEFAULT_DATABASE_PATH = Path("data/rankings.sqlite")


class PipelineError(RuntimeError):
    """Raised when the weekly report cannot complete safely."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and send the weekly GitHub Star Top 10 report.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="send even when this week is already recorded as sent",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run collection, rendering, and database upsert without sending email",
    )
    parser.add_argument(
        "--week-start",
        help="override the UTC week start date for backfills or smoke tests (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH)),
        help="SQLite database path, defaults to data/rankings.sqlite",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="logging verbosity",
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def previous_complete_utc_week(today: date | None = None) -> tuple[date, date]:
    """Return Monday and Sunday for the previous complete UTC week."""

    today = today or datetime.now(timezone.utc).date()
    current_monday = today - timedelta(days=today.weekday())
    week_start = current_monday - timedelta(days=7)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def parse_week_start(value: str | None) -> tuple[date, date]:
    if not value:
        return previous_complete_utc_week()

    try:
        week_start = date.fromisoformat(value)
    except ValueError as exc:
        raise PipelineError("--week-start must use YYYY-MM-DD format") from exc

    if week_start.weekday() != 0:
        raise PipelineError("--week-start must be a Monday in the UTC reporting calendar")

    return week_start, week_start + timedelta(days=6)


@contextmanager
def temporary_google_credentials() -> Iterator[None]:
    """Write service-account JSON to a temp file only for this process.

    Some BigQuery clients require GOOGLE_APPLICATION_CREDENTIALS to point at a
    file. The secret itself should stay in GitHub Actions Secrets or a local
    environment variable and never be committed.
    """

    raw_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    existing_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw_json or existing_path:
        yield
        return

    temp_path: str | None = None
    previous_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="github-star-weekly-",
            delete=False,
        ) as handle:
            handle.write(raw_json)
            temp_path = handle.name
        try:
            Path(temp_path).chmod(0o600)
        except OSError:
            pass
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_path
        yield
    finally:
        if previous_path is None:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = previous_path
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove temporary Google credentials file")


def load_optional_config() -> Any | None:
    try:
        config_module = import_module("src.config")
    except ModuleNotFoundError as exc:
        if exc.name != "src.config":
            raise PipelineError(f"Could not import src.config because dependency {exc.name!r} is missing") from exc
        return None

    for name in ("load_config", "get_config", "Config"):
        candidate = getattr(config_module, name, None)
        if callable(candidate):
            try:
                return candidate()
            except ValueError as exc:
                raise PipelineError(str(exc)) from exc
    return None


def require_callable(module_name: str, candidates: Sequence[str]) -> Callable[..., Any]:
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise PipelineError(
                f"Could not import {module_name!r} because dependency {exc.name!r} is missing"
            ) from exc
        raise PipelineError(
            f"Required module {module_name!r} is missing. "
            "Merge the owning thread's implementation before running the full pipeline."
        ) from exc

    for name in candidates:
        value = getattr(module, name, None)
        if callable(value):
            return value

    joined = ", ".join(candidates)
    raise PipelineError(f"{module_name!r} must expose one of these callables: {joined}")


def call_with_supported_kwargs(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Call a function with the subset of keyword arguments it accepts."""

    signature = inspect.signature(func)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return func(**kwargs)

    supported = {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters
    }
    return func(**supported)


def to_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(f"Cannot convert {type(value).__name__} to a dictionary")


def normalize_repo(raw: Any, rank: int | None = None) -> dict[str, Any]:
    repo = to_dict(raw)
    if rank is not None:
        repo.setdefault("rank", rank)

    full_name = repo.get("full_name") or repo.get("repo") or repo.get("name")
    if not full_name:
        raise PipelineError("Repository item is missing full_name")
    repo["full_name"] = str(full_name)

    html_url = repo.get("html_url") or repo.get("url") or f"https://github.com/{repo['full_name']}"
    repo["html_url"] = str(html_url)

    stars_gained = repo.get("stars_gained", repo.get("weekly_stars", repo.get("star_count", 0)))
    try:
        repo["stars_gained"] = int(stars_gained)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"Invalid stars_gained for {repo['full_name']!r}: {stars_gained!r}") from exc

    total_stars = repo.get("total_stars", repo.get("stargazers_count"))
    if total_stars is not None:
        try:
            repo["total_stars"] = int(total_stars)
        except (TypeError, ValueError):
            repo["total_stars"] = None

    topics = repo.get("topics")
    if isinstance(topics, str):
        try:
            parsed_topics = json.loads(topics)
            if isinstance(parsed_topics, list):
                repo["topics"] = parsed_topics
        except json.JSONDecodeError:
            repo["topics"] = [topic.strip() for topic in topics.split(",") if topic.strip()]
    elif topics is None:
        repo["topics"] = []

    return repo


def normalize_repositories(items: Iterable[Any]) -> list[dict[str, Any]]:
    repositories = [normalize_repo(item, rank=index) for index, item in enumerate(items, start=1)]
    if not repositories:
        raise PipelineError("Collector returned no repositories; refusing to send an empty report")
    return repositories


def normalize_message_id(response: Any) -> str | None:
    if response is None:
        return None
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in ("message_id", "id"):
            if response.get(key):
                return str(response[key])
    for key in ("message_id", "id"):
        value = getattr(response, key, None)
        if value:
            return str(value)
    return None


def config_value(config: Any, *names: str, default: Any = None) -> Any:
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


class DatabaseAdapter:
    def __init__(self, db_module: Any, db_path: Path, config: Any | None) -> None:
        self.db_module = db_module
        self.db_path = db_path
        self.config = config
        self.handle = self._connect()
        self._initialize()

    def _connect(self) -> Any:
        for name in ("connect", "get_connection", "open_database", "Database"):
            candidate = getattr(self.db_module, name, None)
            if callable(candidate):
                return call_with_supported_kwargs(
                    candidate,
                    db_path=self.db_path,
                    database_path=self.db_path,
                    path=self.db_path,
                    config=self.config,
                )
        return None

    def _initialize(self) -> None:
        for name in ("init_db", "initialize_database", "initialize", "ensure_schema"):
            candidate = getattr(self.db_module, name, None)
            if callable(candidate):
                call_with_supported_kwargs(
                    candidate,
                    conn=self.handle,
                    connection=self.handle,
                    db=self.handle,
                    database=self.handle,
                    db_path=self.db_path,
                    database_path=self.db_path,
                    path=self.db_path,
                )
                return

        if self.handle is not None:
            for name in ("init_db", "initialize", "ensure_schema"):
                candidate = getattr(self.handle, name, None)
                if callable(candidate):
                    candidate()
                    return

    def already_sent(self, week_start: date) -> bool:
        week_start_text = week_start.isoformat()
        for owner in (self.db_module, self.handle):
            if owner is None:
                continue
            for name in (
                "has_email_been_sent",
                "has_email_sent",
                "email_sent",
                "is_email_sent",
                "was_email_sent",
            ):
                candidate = getattr(owner, name, None)
                if callable(candidate):
                    return bool(
                        call_with_supported_kwargs(
                            candidate,
                            conn=self.handle,
                            connection=self.handle,
                            db=self.handle,
                            database=self.handle,
                            db_path=self.db_path,
                            database_path=self.db_path,
                            path=self.db_path,
                            week_start=week_start_text,
                        )
                    )
        raise PipelineError("Database module must provide an email sent check")

    def upsert_rankings(self, week_start: date, week_end: date, repositories: list[dict[str, Any]]) -> None:
        for owner in (self.db_module, self.handle):
            if owner is None:
                continue
            for name in ("upsert_weekly_rankings", "upsert_rankings", "save_weekly_rankings", "save_rankings"):
                candidate = getattr(owner, name, None)
                if callable(candidate):
                    call_with_supported_kwargs(
                        candidate,
                        conn=self.handle,
                        connection=self.handle,
                        db=self.handle,
                        database=self.handle,
                        db_path=self.db_path,
                        database_path=self.db_path,
                        path=self.db_path,
                        week_start=week_start.isoformat(),
                        week_end=week_end.isoformat(),
                        repositories=repositories,
                        rankings=repositories,
                        items=repositories,
                    )
                    return
        raise PipelineError("Database module must provide a rankings upsert function")

    def ranking_count(self, full_name: str) -> int:
        for owner in (self.db_module, self.handle):
            if owner is None:
                continue
            for name in ("get_repo_appearance_count", "get_appearance_count", "count_repo_appearances", "ranking_count"):
                candidate = getattr(owner, name, None)
                if callable(candidate):
                    return int(
                        call_with_supported_kwargs(
                            candidate,
                            conn=self.handle,
                            connection=self.handle,
                            db=self.handle,
                            database=self.handle,
                            db_path=self.db_path,
                            database_path=self.db_path,
                            path=self.db_path,
                            full_name=full_name,
                            repo_full_name=full_name,
                        )
                    )
        raise PipelineError("Database module must provide a ranking count function")

    def record_email_sent(
        self,
        week_start: date,
        week_end: date,
        recipient: str,
        message_id: str | None,
        *,
        overwrite: bool = False,
    ) -> None:
        for owner in (self.db_module, self.handle):
            if owner is None:
                continue
            for name in ("record_email_sent", "save_email_send", "mark_email_sent"):
                candidate = getattr(owner, name, None)
                if callable(candidate):
                    call_with_supported_kwargs(
                        candidate,
                        conn=self.handle,
                        connection=self.handle,
                        db=self.handle,
                        database=self.handle,
                        db_path=self.db_path,
                        database_path=self.db_path,
                        path=self.db_path,
                        week_start=week_start.isoformat(),
                        week_end=week_end.isoformat(),
                        recipient=recipient,
                        email_to=recipient,
                        message_id=message_id,
                        overwrite=overwrite,
                    )
                    return
        raise PipelineError("Database module must provide an email sent recorder")

    def close(self) -> None:
        if self.handle is not None and hasattr(self.handle, "close"):
            self.handle.close()


def collect_top_repositories(week_start: date, week_end: date, config: Any | None) -> list[dict[str, Any]]:
    try:
        collect_module = import_module("src.collect")
    except ModuleNotFoundError as exc:
        if exc.name != "src.collect":
            raise PipelineError(
                f"Could not import 'src.collect' because dependency {exc.name!r} is missing"
            ) from exc
        raise PipelineError(
            "Required module 'src.collect' is missing. Merge thread B before running the full pipeline."
        ) from exc

    collect = None
    for name in (
        "collect_weekly_top_repos",
        "collect_top_repositories",
        "get_top_repositories",
        "fetch_top_repos",
        "collect_weekly_top",
    ):
        candidate = getattr(collect_module, name, None)
        if callable(candidate):
            collect = candidate
            break
    if collect is None:
        raise PipelineError("src.collect must expose a weekly repository collector")

    window = None
    week_window = getattr(collect_module, "WeekWindow", None)
    if callable(week_window):
        window = week_window(start=week_start, end=week_end)

    raw_items = call_with_supported_kwargs(
        collect,
        window=window,
        week_start=week_start,
        week_end=week_end,
        start_date=week_start,
        end_date=week_end,
        limit=10,
        config=config,
    )
    return normalize_repositories(raw_items)


def enrich_repositories(repositories: list[dict[str, Any]], config: Any | None) -> list[dict[str, Any]]:
    enrich = require_callable(
        "src.enrich",
        ("enrich_repositories", "enrich_repos", "fetch_repository_details"),
    )
    enriched = call_with_supported_kwargs(
        enrich,
        repositories=repositories,
        repos=repositories,
        items=repositories,
        config=config,
    )
    return normalize_repositories(enriched)


def summarize_repositories(repositories: list[dict[str, Any]], config: Any | None) -> list[dict[str, Any]]:
    try:
        summarize_module = import_module("src.summarize")
    except ModuleNotFoundError as exc:
        if exc.name != "src.summarize":
            raise PipelineError(
                f"Could not import 'src.summarize' because dependency {exc.name!r} is missing"
            ) from exc
        raise PipelineError(
            "Required module 'src.summarize' is missing. "
            "Merge thread D before running the full pipeline."
        ) from exc

    batch_summarizer = None
    for name in ("summarize_repositories", "summarize_repos", "generate_summaries"):
        candidate = getattr(summarize_module, name, None)
        if callable(candidate):
            batch_summarizer = candidate
            break

    if batch_summarizer is not None:
        summarized = call_with_supported_kwargs(
            batch_summarizer,
            repositories=repositories,
            repos=repositories,
            items=repositories,
            config=config,
        )
        return normalize_repositories(summarized)

    single_summarizer = None
    for name in ("summarize_repository", "summarize_repo", "generate_summary"):
        candidate = getattr(summarize_module, name, None)
        if callable(candidate):
            single_summarizer = candidate
            break

    if single_summarizer is None:
        raise PipelineError("src.summarize must expose a batch or per-repository summarizer")

    summarized_repositories: list[dict[str, Any]] = []
    for repo in repositories:
        updated = dict(repo)
        summary = call_with_supported_kwargs(
            single_summarizer,
            repository=updated,
            repo=updated,
            item=updated,
            config=config,
        )
        if isinstance(summary, Mapping):
            updated.update(summary)
        elif summary:
            updated["summary_zh"] = str(summary)
        summarized_repositories.append(updated)
    return normalize_repositories(summarized_repositories)


def render_email(
    repositories: list[dict[str, Any]],
    week_start: date,
    week_end: date,
    config: Any | None,
) -> str:
    render = require_callable(
        "src.render",
        ("render_weekly_email", "render_email", "render"),
    )
    html = call_with_supported_kwargs(
        render,
        repositories=repositories,
        repos=repositories,
        items=repositories,
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        config=config,
    )
    if not isinstance(html, str) or not html.strip():
        raise PipelineError("Rendered email HTML is empty")
    return html


def send_email(
    html: str,
    repositories: list[dict[str, Any]],
    week_start: date,
    week_end: date,
    config: Any | None,
    *,
    force: bool = False,
) -> str | None:
    emailer = require_callable(
        "src.emailer",
        ("send_weekly_email", "send_email", "send"),
    )
    subject = f"GitHub 本周 Star 增长最快 Top 10（{week_start.isoformat()} 至 {week_end.isoformat()}）"
    idempotency_key = f"github-star-weekly:{week_start.isoformat()}"
    if force:
        idempotency_key = f"{idempotency_key}:force:{datetime.now(timezone.utc).isoformat()}"
    response = call_with_supported_kwargs(
        emailer,
        html=html,
        html_body=html,
        subject=subject,
        repositories=repositories,
        repos=repositories,
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        idempotency_key=idempotency_key,
        force=force,
        config=config,
    )
    return normalize_message_id(response)


def build_database(db_path: Path, config: Any | None) -> DatabaseAdapter:
    try:
        db_module = import_module("src.db")
    except ModuleNotFoundError as exc:
        if exc.name != "src.db":
            raise PipelineError(f"Could not import 'src.db' because dependency {exc.name!r} is missing") from exc
        raise PipelineError(
            "Required module 'src.db' is missing. Merge thread C before running the full pipeline."
        ) from exc

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return DatabaseAdapter(db_module, db_path, config)


def checkpoint_sqlite_database(db_path: Path) -> None:
    """Flush SQLite WAL state so GitHub Actions can commit one database file."""

    if str(db_path) == ":memory:" or not db_path.exists():
        return
    try:
        with closing(sqlite3.connect(str(db_path), timeout=30)) as connection:
            cursor = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cursor.fetchall()
    except sqlite3.Error as exc:
        LOGGER.warning("Could not checkpoint SQLite database before exit: %s", exc)


def recipient_text(config: Any | None) -> str:
    value = config_value(
        config,
        "email_to",
        "EMAIL_TO",
        default=os.getenv("EMAIL_TO", ""),
    )
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    return str(value)


def run_pipeline(args: argparse.Namespace) -> int:
    week_start, week_end = parse_week_start(args.week_start)
    db_path = Path(args.db_path)

    LOGGER.info("Preparing weekly report for %s through %s", week_start, week_end)
    config = load_optional_config()

    with temporary_google_credentials():
        database = build_database(db_path, config)
        try:
            if database.already_sent(week_start) and not args.force and not args.dry_run:
                LOGGER.info(
                    "Email for week_start=%s is already recorded as sent; use --force to resend",
                    week_start,
                )
                return 0

            repositories = collect_top_repositories(week_start, week_end, config)
            repositories = enrich_repositories(repositories, config)
            repositories = summarize_repositories(repositories, config)

            database.upsert_rankings(week_start, week_end, repositories)

            for repo in repositories:
                repo["appearance_count"] = database.ranking_count(repo["full_name"])

            html = render_email(repositories, week_start, week_end, config)

            if args.dry_run:
                LOGGER.info("Dry run completed after rendering email; no email was sent")
                return 0

            message_id = send_email(
                html,
                repositories,
                week_start,
                week_end,
                config,
                force=args.force,
            )
            database.record_email_sent(
                week_start,
                week_end,
                recipient_text(config),
                message_id,
                overwrite=args.force,
            )
            LOGGER.info("Weekly report sent successfully; message_id=%s", message_id or "unknown")
            return 0
        finally:
            database.close()
            checkpoint_sqlite_database(db_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        return run_pipeline(args)
    except PipelineError as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Unexpected failure while running weekly report")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
