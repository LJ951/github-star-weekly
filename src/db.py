"""SQLite persistence for rankings history and email send idempotency."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_DATABASE_PATH = Path("data/rankings.sqlite")
SCHEMA_VERSION = 1
TOP_RANK_LIMIT = 10


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed safely."""


@dataclass(frozen=True)
class RankingRecord:
    """Normalized weekly ranking row."""

    week_start: str
    week_end: str
    rank: int
    full_name: str
    html_url: str
    stars_gained: int
    total_stars: int | None = None
    language: str | None = None
    description: str | None = None
    topics: tuple[str, ...] = ()
    summary_zh: str | None = None


def init_db(database_path: str | Path = DEFAULT_DATABASE_PATH) -> Path:
    """Create the SQLite database file and required tables if needed."""

    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
    return path


def upsert_weekly_rankings(
    database_path: str | Path,
    week_start: str | date,
    week_end: str | date,
    rankings: Sequence[Mapping[str, Any] | RankingRecord],
    *,
    prune_missing: bool = True,
) -> int:
    """Save a weekly Top N ranking batch atomically.

    Existing rows for the same ``week_start`` and repository are updated. By
    default, rows for the same week that are not present in the current batch
    are removed, so rerunning a corrected week leaves exactly one coherent
    weekly list.
    """

    normalized_week_start = _coerce_date(week_start, "week_start")
    normalized_week_end = _coerce_date(week_end, "week_end")
    records = _normalize_rankings(
        normalized_week_start,
        normalized_week_end,
        rankings,
    )

    path = _database_path(database_path)
    now = _utc_now()
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO weekly_rankings (
                      week_start,
                      week_end,
                      rank,
                      full_name,
                      html_url,
                      stars_gained,
                      total_stars,
                      language,
                      description,
                      topics,
                      summary_zh,
                      updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(week_start, full_name) DO UPDATE SET
                      week_end = excluded.week_end,
                      rank = excluded.rank,
                      html_url = excluded.html_url,
                      stars_gained = excluded.stars_gained,
                      total_stars = excluded.total_stars,
                      language = excluded.language,
                      description = excluded.description,
                      topics = excluded.topics,
                      summary_zh = excluded.summary_zh,
                      updated_at = excluded.updated_at
                    """,
                    [
                        (
                            record.week_start,
                            record.week_end,
                            record.rank,
                            record.full_name,
                            record.html_url,
                            record.stars_gained,
                            record.total_stars,
                            record.language,
                            record.description,
                            _topics_to_json(record.topics),
                            record.summary_zh,
                            now,
                        )
                        for record in records
                    ],
                )

                if prune_missing:
                    placeholders = ", ".join("?" for _ in records)
                    connection.execute(
                        f"""
                        DELETE FROM weekly_rankings
                        WHERE week_start = ?
                          AND full_name NOT IN ({placeholders})
                        """,
                        [normalized_week_start]
                        + [record.full_name for record in records],
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to save weekly rankings: {exc}") from exc

    return len(records)


def get_weekly_rankings(
    database_path: str | Path,
    week_start: str | date,
    *,
    include_appearance_counts: bool = False,
) -> list[dict[str, Any]]:
    """Return rankings for one week ordered by rank."""

    normalized_week_start = _coerce_date(week_start, "week_start")
    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        if include_appearance_counts:
            sql = """
                SELECT
                  wr.*,
                  (
                    SELECT COUNT(*)
                    FROM weekly_rankings AS history
                    WHERE history.full_name = wr.full_name
                      AND history.rank <= ?
                  ) AS appearance_count
                FROM weekly_rankings AS wr
                WHERE wr.week_start = ?
                ORDER BY wr.rank ASC, wr.full_name ASC
            """
            params: tuple[Any, ...] = (TOP_RANK_LIMIT, normalized_week_start)
        else:
            sql = """
                SELECT *
                FROM weekly_rankings
                WHERE week_start = ?
                ORDER BY rank ASC, full_name ASC
            """
            params = (normalized_week_start,)

        try:
            rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to load weekly rankings: {exc}") from exc

    return [_row_to_dict(row) for row in rows]


def get_appearance_count(
    database_path: str | Path,
    full_name: str,
    *,
    rank_limit: int = TOP_RANK_LIMIT,
) -> int:
    """Return cumulative chart appearances for one repository."""

    full_name = _required_text(full_name, "full_name")
    counts = get_appearance_counts(database_path, [full_name], rank_limit=rank_limit)
    return counts.get(full_name, 0)


def get_appearance_counts(
    database_path: str | Path,
    full_names: Iterable[str],
    *,
    rank_limit: int = TOP_RANK_LIMIT,
) -> dict[str, int]:
    """Return cumulative chart appearances, including any saved current week."""

    if rank_limit <= 0:
        raise DatabaseError("rank_limit must be greater than 0")

    names = [_required_text(name, "full_name") for name in full_names]
    if not names:
        return {}

    placeholders = ", ".join("?" for _ in names)
    sql = f"""
        SELECT full_name, COUNT(*) AS appearance_count
        FROM weekly_rankings
        WHERE rank <= ?
          AND full_name IN ({placeholders})
        GROUP BY full_name
    """
    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            rows = connection.execute(sql, [rank_limit] + names).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to load appearance counts: {exc}") from exc

    return {row["full_name"]: int(row["appearance_count"]) for row in rows}


def has_email_been_sent(
    database_path: str | Path,
    week_start: str | date,
) -> bool:
    """Return True when a successful send is already recorded for this week."""

    return get_email_send(database_path, week_start) is not None


def get_email_send(
    database_path: str | Path,
    week_start: str | date,
) -> dict[str, Any] | None:
    """Return the recorded email send for one week, if present."""

    normalized_week_start = _coerce_date(week_start, "week_start")
    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            row = connection.execute(
                """
                SELECT *
                FROM email_sends
                WHERE week_start = ?
                """,
                (normalized_week_start,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to load email send record: {exc}") from exc

    if row is None:
        return None
    return dict(row)


def record_email_sent(
    database_path: str | Path,
    week_start: str | date,
    week_end: str | date,
    recipient: str,
    *,
    message_id: str | None = None,
    sent_at: str | datetime | None = None,
    overwrite: bool = False,
) -> bool:
    """Record a successful email send.

    Returns True when a row was inserted or updated. Returns False when a send
    for the same week already exists and ``overwrite`` is False.
    """

    normalized_week_start = _coerce_date(week_start, "week_start")
    normalized_week_end = _coerce_date(week_end, "week_end")
    normalized_recipient = _required_text(recipient, "recipient")
    normalized_sent_at = _coerce_datetime(sent_at) if sent_at else _utc_now()
    normalized_message_id = _optional_text(message_id)

    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            with connection:
                if overwrite:
                    cursor = connection.execute(
                        """
                        INSERT INTO email_sends (
                          week_start,
                          week_end,
                          sent_at,
                          recipient,
                          message_id
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(week_start) DO UPDATE SET
                          week_end = excluded.week_end,
                          sent_at = excluded.sent_at,
                          recipient = excluded.recipient,
                          message_id = excluded.message_id
                        """,
                        (
                            normalized_week_start,
                            normalized_week_end,
                            normalized_sent_at,
                            normalized_recipient,
                            normalized_message_id,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO email_sends (
                          week_start,
                          week_end,
                          sent_at,
                          recipient,
                          message_id
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_week_start,
                            normalized_week_end,
                            normalized_sent_at,
                            normalized_recipient,
                            normalized_message_id,
                        ),
                    )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to record email send: {exc}") from exc

    return cursor.rowcount > 0


def clear_email_send(
    database_path: str | Path,
    week_start: str | date,
) -> bool:
    """Delete one send record, useful for explicit forced resend workflows."""

    normalized_week_start = _coerce_date(week_start, "week_start")
    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM email_sends WHERE week_start = ?",
                    (normalized_week_start,),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to clear email send record: {exc}") from exc

    return cursor.rowcount > 0


def get_schema_version(database_path: str | Path = DEFAULT_DATABASE_PATH) -> int:
    """Return the SQLite user_version used by this module."""

    path = _database_path(database_path)
    with _open_database(path) as connection:
        _apply_schema(connection)
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to read schema version: {exc}") from exc
    return int(row[0])


@contextmanager
def _open_database(database_path: Path) -> Any:
    connection: sqlite3.Connection | None = None
    try:
        if database_path != Path(":memory:"):
            database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        yield connection
    except sqlite3.Error as exc:
        raise DatabaseError(f"SQLite operation failed for {database_path}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _apply_schema(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS weekly_rankings (
              week_start TEXT NOT NULL,
              week_end TEXT NOT NULL,
              rank INTEGER NOT NULL CHECK(rank >= 1),
              full_name TEXT NOT NULL CHECK(length(trim(full_name)) > 0),
              html_url TEXT NOT NULL CHECK(length(trim(html_url)) > 0),
              stars_gained INTEGER NOT NULL CHECK(stars_gained >= 0),
              total_stars INTEGER CHECK(total_stars IS NULL OR total_stars >= 0),
              language TEXT,
              description TEXT,
              topics TEXT NOT NULL DEFAULT '[]',
              summary_zh TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (week_start, full_name)
            );

            CREATE INDEX IF NOT EXISTS idx_weekly_rankings_week_rank
              ON weekly_rankings(week_start, rank);

            CREATE INDEX IF NOT EXISTS idx_weekly_rankings_full_name_rank
              ON weekly_rankings(full_name, rank);

            CREATE TABLE IF NOT EXISTS email_sends (
              week_start TEXT PRIMARY KEY,
              week_end TEXT NOT NULL,
              sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              recipient TEXT NOT NULL CHECK(length(trim(recipient)) > 0),
              message_id TEXT
            );
            """
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _normalize_rankings(
    week_start: str,
    week_end: str,
    rankings: Sequence[Mapping[str, Any] | RankingRecord],
) -> list[RankingRecord]:
    if not rankings:
        raise DatabaseError("rankings must contain at least one item")

    records = [
        _normalize_ranking(week_start, week_end, ranking, position)
        for position, ranking in enumerate(rankings, start=1)
    ]
    seen_names: set[str] = set()
    seen_ranks: set[int] = set()
    for record in records:
        if record.full_name in seen_names:
            raise DatabaseError(f"Duplicate repository in ranking batch: {record.full_name}")
        if record.rank in seen_ranks:
            raise DatabaseError(f"Duplicate rank in ranking batch: {record.rank}")
        seen_names.add(record.full_name)
        seen_ranks.add(record.rank)
    return records


def _normalize_ranking(
    week_start: str,
    week_end: str,
    ranking: Mapping[str, Any] | RankingRecord,
    position: int,
) -> RankingRecord:
    if isinstance(ranking, RankingRecord):
        return RankingRecord(
            week_start=week_start,
            week_end=week_end,
            rank=_positive_int(ranking.rank, "rank"),
            full_name=_required_text(ranking.full_name, "full_name"),
            html_url=_required_text(ranking.html_url, "html_url"),
            stars_gained=_non_negative_int(ranking.stars_gained, "stars_gained"),
            total_stars=_optional_non_negative_int(ranking.total_stars, "total_stars"),
            language=_optional_text(ranking.language),
            description=_optional_text(ranking.description),
            topics=_normalize_topics(ranking.topics),
            summary_zh=_optional_text(ranking.summary_zh),
        )

    full_name = _required_text(ranking.get("full_name"), "full_name")
    html_url = _optional_text(ranking.get("html_url")) or f"https://github.com/{full_name}"
    total_stars = ranking.get("total_stars", ranking.get("stargazers_count"))

    return RankingRecord(
        week_start=week_start,
        week_end=week_end,
        rank=_positive_int(ranking.get("rank", position), "rank"),
        full_name=full_name,
        html_url=_required_text(html_url, "html_url"),
        stars_gained=_non_negative_int(ranking.get("stars_gained"), "stars_gained"),
        total_stars=_optional_non_negative_int(total_stars, "total_stars"),
        language=_optional_text(ranking.get("language")),
        description=_optional_text(ranking.get("description")),
        topics=_normalize_topics(ranking.get("topics")),
        summary_zh=_optional_text(ranking.get("summary_zh")),
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["topics"] = _topics_from_json(value.get("topics"))
    return value


def _database_path(database_path: str | Path) -> Path:
    if isinstance(database_path, Path):
        return database_path
    if not str(database_path).strip():
        raise DatabaseError("database_path must not be empty")
    return Path(database_path)


def _coerce_date(value: str | date, field_name: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise DatabaseError(f"{field_name} must be an ISO date string")
    normalized = value.strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise DatabaseError(f"{field_name} must use YYYY-MM-DD format") from exc


def _coerce_datetime(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    if not isinstance(value, str):
        raise DatabaseError("sent_at must be an ISO datetime string")
    normalized = value.strip()
    if not normalized:
        raise DatabaseError("sent_at must not be empty")
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatabaseError("sent_at must be a valid ISO datetime string") from exc
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise DatabaseError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise DatabaseError(f"{field_name} must be a non-empty string")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    return normalized or None


def _positive_int(value: Any, field_name: str) -> int:
    integer = _coerce_int(value, field_name)
    if integer <= 0:
        raise DatabaseError(f"{field_name} must be greater than 0")
    return integer


def _non_negative_int(value: Any, field_name: str) -> int:
    integer = _coerce_int(value, field_name)
    if integer < 0:
        raise DatabaseError(f"{field_name} must be greater than or equal to 0")
    return integer


def _optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, field_name)


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise DatabaseError(f"{field_name} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f"{field_name} must be an integer") from exc
    return integer


def _normalize_topics(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return (stripped,)
        return _normalize_topics(parsed)
    if not isinstance(value, Iterable):
        raise DatabaseError("topics must be a list of strings")
    topics: list[str] = []
    for topic in value:
        normalized = _optional_text(topic)
        if normalized is not None:
            topics.append(normalized)
    return tuple(dict.fromkeys(topics))


def _topics_to_json(topics: Iterable[str]) -> str:
    return json.dumps(list(topics), ensure_ascii=False, separators=(",", ":"))


def _topics_from_json(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [value]
    if not isinstance(parsed, list):
        return []
    return [str(topic) for topic in parsed if str(topic).strip()]


if __name__ == "__main__":
    created_path = init_db()
    print(f"SQLite database is ready: {created_path}")
