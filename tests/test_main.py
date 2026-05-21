from __future__ import annotations

import argparse
import tempfile
import types
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from src import main


class FakeDatabase:
    def already_sent(self, week_start: date) -> bool:
        return False

    def upsert_rankings(self, week_start: date, week_end: date, repositories: list[dict]) -> None:
        return None

    def ranking_count(self, full_name: str) -> int:
        return 1

    def record_email_sent(
        self,
        week_start: date,
        week_end: date,
        recipient: str,
        message_id: str | None,
        *,
        overwrite: bool = False,
    ) -> None:
        raise AssertionError("dry-run mode must not record email sends")

    def close(self) -> None:
        return None


class MainTests(unittest.TestCase):
    def test_collect_uses_top_n_from_config(self) -> None:
        calls: dict[str, int] = {}

        def collect_top_repositories(**kwargs):
            calls["limit"] = kwargs["limit"]
            return [{"full_name": "owner/repo", "stars_gained": 10}]

        fake_collect_module = types.SimpleNamespace(
            collect_top_repositories=collect_top_repositories
        )

        with patch.object(main, "import_module", return_value=fake_collect_module):
            repositories = main.collect_top_repositories(
                date(2026, 5, 11),
                date(2026, 5, 17),
                {"top_n": 3},
            )

        self.assertEqual(calls["limit"], 3)
        self.assertEqual(repositories[0]["full_name"], "owner/repo")

    def test_config_bool_parses_false_string_as_false(self) -> None:
        self.assertFalse(main.config_bool({"dry_run": "false"}, "dry_run"))
        self.assertTrue(main.config_bool({"dry_run": "true"}, "dry_run"))

    def test_dry_run_from_config_skips_send_and_send_record(self) -> None:
        send_email = Mock(side_effect=AssertionError("dry-run mode must not send email"))
        args = argparse.Namespace(
            week_start="2026-05-11",
            db_path=str(Path(tempfile.gettempdir()) / "github-star-weekly-test.sqlite"),
            force=False,
            dry_run=False,
        )

        with patch.object(main, "load_optional_config", return_value={"dry_run": True, "email_to": "reader@example.com"}), \
            patch.object(main, "temporary_google_credentials", return_value=nullcontext()), \
            patch.object(main, "build_database", return_value=FakeDatabase()), \
            patch.object(main, "collect_top_repositories", return_value=[{"full_name": "owner/repo", "stars_gained": 10}]), \
            patch.object(main, "enrich_repositories", side_effect=lambda repositories, config: repositories), \
            patch.object(main, "summarize_repositories", side_effect=lambda repositories, config: repositories), \
            patch.object(main, "render_email", return_value="<html>report</html>"), \
            patch.object(main, "send_email", send_email):
            exit_code = main.run_pipeline(args)

        self.assertEqual(exit_code, 0)
        send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
