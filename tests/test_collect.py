from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import collect


class FakeQueryParameter:
    def __init__(self, name: str, type_: str, value: object) -> None:
        self.name = name
        self.type_ = type_
        self.value = value


class FakeQueryJobConfig:
    def __init__(
        self,
        *,
        query_parameters: list[FakeQueryParameter] | None = None,
        dry_run: bool = False,
        use_query_cache: bool | None = None,
    ) -> None:
        self.query_parameters = query_parameters or []
        self.dry_run = dry_run
        self.use_query_cache = use_query_cache


class FakeJob:
    def __init__(
        self,
        rows: list[object] | None = None,
        *,
        total_bytes_processed: int | None = None,
    ) -> None:
        self._rows = rows or []
        self.total_bytes_processed = total_bytes_processed

    def result(self) -> list[object]:
        return self._rows


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def query(self, query: str, job_config: object | None = None) -> FakeJob:
        self.calls.append(SimpleNamespace(query=query, job_config=job_config))
        if getattr(job_config, "dry_run", False):
            return FakeJob(total_bytes_processed=1024)
        return FakeJob(
            [
                SimpleNamespace(full_name="octocat/hello-world", stars_gained=123),
                {"full_name": "python/cpython", "stars_gained": 100},
            ]
        )


class CollectTests(unittest.TestCase):
    def test_previous_complete_utc_week_returns_monday_to_sunday(self) -> None:
        window = collect.previous_complete_utc_week(
            datetime(2026, 5, 21, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(window.start, date(2026, 5, 11))
        self.assertEqual(window.end, date(2026, 5, 17))
        self.assertEqual(window.start_suffix, "20260511")
        self.assertEqual(window.end_suffix, "20260517")
        self.assertEqual(
            window.suffixes,
            [
                "20260511",
                "20260512",
                "20260513",
                "20260514",
                "20260515",
                "20260516",
                "20260517",
            ],
        )

    def test_validate_week_window_rejects_more_than_seven_days(self) -> None:
        with self.assertRaises(ValueError):
            collect.validate_week_window(
                collect.WeekWindow(start=date(2026, 5, 1), end=date(2026, 5, 8))
            )

    def test_sql_uses_explicit_day_tables_and_parameterized_limit(self) -> None:
        sql = collect.build_weekly_top_repos_sql(
            collect.WeekWindow(start=date(2026, 5, 11), end=date(2026, 5, 17))
        )

        self.assertIn("@limit", sql)
        self.assertIn("type = 'WatchEvent'", sql)
        self.assertIn("githubarchive.day.20260511", sql)
        self.assertIn("githubarchive.day.20260517", sql)
        self.assertNotIn("githubarchive.day.*", sql)
        self.assertEqual(sql.count("UNION ALL"), 6)

    def test_collect_runs_dry_run_and_parameterized_query(self) -> None:
        fake_bigquery = SimpleNamespace(
            ScalarQueryParameter=FakeQueryParameter,
            QueryJobConfig=FakeQueryJobConfig,
            Client=lambda: FakeClient(),
        )
        client = FakeClient()

        with patch.object(collect, "_load_bigquery_module", return_value=fake_bigquery):
            results = collect.collect_weekly_top_repos(
                client,
                window=collect.WeekWindow(
                    start=date(2026, 5, 11), end=date(2026, 5, 17)
                ),
                limit=2,
            )

        self.assertEqual(
            results,
            [
                collect.RepoStarCount("octocat/hello-world", 123),
                collect.RepoStarCount("python/cpython", 100),
            ],
        )
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0].job_config.dry_run)
        params = {param.name: param.value for param in client.calls[1].job_config.query_parameters}
        self.assertEqual(params, {"limit": 2})

    def test_main_compatible_entrypoint_accepts_week_start_and_config(self) -> None:
        fake_bigquery = SimpleNamespace(
            ScalarQueryParameter=FakeQueryParameter,
            QueryJobConfig=FakeQueryJobConfig,
            Client=lambda: FakeClient(),
        )
        client = FakeClient()

        with patch.object(collect, "_load_bigquery_module", return_value=fake_bigquery):
            results = collect.collect_top_repositories(
                week_start="2026-05-11",
                week_end="2026-05-17",
                client=client,
                config={"top_n": 1},
            )

        self.assertEqual(len(results), 2)
        params = {param.name: param.value for param in client.calls[1].job_config.query_parameters}
        self.assertEqual(params["limit"], 1)


if __name__ == "__main__":
    unittest.main()
