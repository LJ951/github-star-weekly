"""Collect weekly GitHub star growth rankings from GH Archive BigQuery data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any, Iterable, Protocol

LOGGER = logging.getLogger(__name__)

DEFAULT_LIMIT = 10
MAX_QUERY_DAYS = 7
GH_ARCHIVE_TABLE = "githubarchive.day.*"


class BigQueryClient(Protocol):
    """Small protocol for the BigQuery client methods used by this module."""

    def query(self, query: str, job_config: object | None = None) -> object:
        ...


@dataclass(frozen=True)
class WeekWindow:
    """Inclusive UTC date window for a complete statistics week."""

    start: date
    end: date

    @property
    def start_suffix(self) -> str:
        return self.start.strftime("%Y%m%d")

    @property
    def end_suffix(self) -> str:
        return self.end.strftime("%Y%m%d")

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class RepoStarCount:
    """A repository and its WatchEvent count for the statistics window."""

    full_name: str
    stars_gained: int


class CollectionError(RuntimeError):
    """Raised when weekly ranking collection cannot complete."""


def previous_complete_utc_week(now: datetime | date | None = None) -> WeekWindow:
    """Return the previous Monday-Sunday UTC week.

    The scheduled job runs on Monday. Using the previous complete UTC week keeps
    the query deterministic when the job is retried later in the same week.
    """

    if now is None:
        current_date = datetime.now(timezone.utc).date()
    elif isinstance(now, datetime):
        current_date = now.astimezone(timezone.utc).date() if now.tzinfo else now.date()
    else:
        current_date = now

    this_week_monday = current_date - timedelta(days=current_date.weekday())
    start = this_week_monday - timedelta(days=7)
    end = this_week_monday - timedelta(days=1)
    return WeekWindow(start=start, end=end)


def validate_week_window(window: WeekWindow) -> None:
    """Ensure the BigQuery scan cannot accidentally exceed the first-version scope."""

    if window.end < window.start:
        raise ValueError("week_end must be on or after week_start")
    if window.day_count > MAX_QUERY_DAYS:
        raise ValueError(
            f"BigQuery collection is limited to {MAX_QUERY_DAYS} days; "
            f"got {window.day_count} days from {window.start} to {window.end}"
        )


def build_weekly_top_repos_sql(table: str = GH_ARCHIVE_TABLE) -> str:
    """Build the parameterized GH Archive query."""

    return f"""
SELECT
  repo.name AS full_name,
  COUNT(*) AS stars_gained
FROM `{table}`
WHERE
  _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
  AND type = 'WatchEvent'
  AND repo.name IS NOT NULL
GROUP BY full_name
ORDER BY stars_gained DESC, full_name ASC
LIMIT @limit
""".strip()


def collect_weekly_top_repos(
    client: BigQueryClient | None = None,
    *,
    window: WeekWindow | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = True,
) -> list[RepoStarCount]:
    """Query GH Archive and return repositories with the most weekly WatchEvents.

    Args:
        client: Optional BigQuery client. When omitted, a real client is created.
        window: Inclusive UTC date window. Defaults to the previous complete week.
        limit: Number of repositories to return.
        dry_run: Whether to run a dry run first and log estimated bytes scanned.

    Raises:
        CollectionError: Wraps BigQuery or dependency errors with context.
        ValueError: For unsafe windows or invalid limits.
    """

    if limit <= 0:
        raise ValueError("limit must be greater than 0")

    query_window = window or previous_complete_utc_week()
    validate_week_window(query_window)

    try:
        bigquery = _load_bigquery_module()
        query = build_weekly_top_repos_sql()
        query_parameters = [
            bigquery.ScalarQueryParameter(
                "start_suffix", "STRING", query_window.start_suffix
            ),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", query_window.end_suffix),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]

        bq_client = client or bigquery.Client()

        if dry_run:
            dry_run_config = bigquery.QueryJobConfig(
                query_parameters=query_parameters,
                dry_run=True,
                use_query_cache=False,
            )
            dry_run_job = bq_client.query(query, job_config=dry_run_config)
            bytes_processed = getattr(dry_run_job, "total_bytes_processed", None)
            if bytes_processed is not None:
                LOGGER.info(
                    "BigQuery dry run for %s to %s estimates %.2f MiB scanned",
                    query_window.start,
                    query_window.end,
                    bytes_processed / 1024 / 1024,
                )

        job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
        rows = bq_client.query(query, job_config=job_config).result()
        return _rows_to_star_counts(rows)
    except ImportError as exc:
        raise CollectionError(
            "google-cloud-bigquery is required for collecting weekly rankings"
        ) from exc
    except Exception as exc:
        raise CollectionError(
            f"Failed to collect GitHub Star ranking for "
            f"{query_window.start} to {query_window.end}: {exc}"
        ) from exc


def collect_top_repositories(
    *,
    week_start: date | str | None = None,
    week_end: date | str | None = None,
    limit: int = DEFAULT_LIMIT,
    config: Any | None = None,
    client: BigQueryClient | None = None,
    dry_run: bool = True,
) -> list[RepoStarCount]:
    """Compatibility entrypoint used by the main orchestration module."""

    resolved_limit = int(_config_value(config, "top_n", "TOP_N", default=limit) or limit)
    resolved_dry_run = bool(_config_value(config, "bigquery_dry_run", default=dry_run))
    window = (
        WeekWindow(start=_coerce_date(week_start), end=_coerce_date(week_end))
        if week_start is not None and week_end is not None
        else previous_complete_utc_week()
    )
    return collect_weekly_top_repos(
        client,
        window=window,
        limit=resolved_limit,
        dry_run=resolved_dry_run,
    )


get_top_repositories = collect_top_repositories
fetch_top_repos = collect_top_repositories
collect_weekly_top = collect_top_repositories


def _rows_to_star_counts(rows: Iterable[object]) -> list[RepoStarCount]:
    rankings: list[RepoStarCount] = []
    for row in rows:
        full_name = _row_value(row, "full_name")
        stars_gained = _row_value(row, "stars_gained")
        if not full_name:
            continue
        rankings.append(
            RepoStarCount(full_name=str(full_name), stars_gained=int(stars_gained or 0))
        )
    return rankings


def _row_value(row: object, key: str) -> object:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _load_bigquery_module() -> object:
    from google.cloud import bigquery

    return bigquery


def _coerce_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError("date value is required")


def _config_value(config: Any | None, *names: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
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
    for item in collect_weekly_top_repos():
        print(f"{item.full_name}\t{item.stars_gained}")
