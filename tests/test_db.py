from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
import sqlite3
from pathlib import Path

from src.db import (
    DatabaseError,
    get_appearance_count,
    get_appearance_counts,
    get_email_send,
    get_schema_version,
    get_weekly_rankings,
    has_email_been_sent,
    init_db,
    record_email_sent,
    upsert_weekly_rankings,
)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "nested" / "rankings.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_db_creates_parent_directory_and_tables(self) -> None:
        init_db(self.db_path)

        self.assertTrue(self.db_path.exists())
        self.assertEqual(get_schema_version(self.db_path), 1)

        with closing(sqlite3.connect(self.db_path)) as connection:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertIn("weekly_rankings", table_names)
        self.assertIn("email_sends", table_names)

    def test_upsert_weekly_rankings_updates_existing_rows(self) -> None:
        first_batch = [
            {
                "rank": 1,
                "full_name": "owner/alpha",
                "stars_gained": 100,
                "total_stars": 1000,
                "language": "Python",
                "description": "Alpha project",
                "topics": ["ai", "tooling"],
                "summary_zh": "Alpha summary",
            },
            {
                "rank": 2,
                "full_name": "owner/beta",
                "html_url": "https://example.com/beta",
                "stars_gained": 90,
                "total_stars": 900,
            },
        ]
        second_batch = [
            {
                "rank": 1,
                "full_name": "owner/beta",
                "stars_gained": 120,
                "total_stars": 950,
                "language": "TypeScript",
                "topics": '["web","frontend"]',
            },
            {
                "rank": 2,
                "full_name": "owner/alpha",
                "stars_gained": 110,
                "total_stars": 1010,
                "language": "Python",
                "topics": ["ai"],
            },
        ]

        self.assertEqual(
            upsert_weekly_rankings(
                self.db_path,
                "2026-05-11",
                "2026-05-17",
                first_batch,
            ),
            2,
        )
        self.assertEqual(
            upsert_weekly_rankings(
                self.db_path,
                "2026-05-11",
                "2026-05-17",
                second_batch,
            ),
            2,
        )

        rows = get_weekly_rankings(self.db_path, "2026-05-11")

        self.assertEqual([row["full_name"] for row in rows], ["owner/beta", "owner/alpha"])
        self.assertEqual(rows[0]["stars_gained"], 120)
        self.assertEqual(rows[0]["html_url"], "https://github.com/owner/beta")
        self.assertEqual(rows[0]["topics"], ["web", "frontend"])
        self.assertEqual(rows[1]["rank"], 2)
        self.assertEqual(rows[1]["topics"], ["ai"])

    def test_upsert_prunes_rows_missing_from_rerun_by_default(self) -> None:
        upsert_weekly_rankings(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            [
                {"rank": 1, "full_name": "owner/alpha", "stars_gained": 100},
                {"rank": 2, "full_name": "owner/beta", "stars_gained": 90},
            ],
        )
        upsert_weekly_rankings(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            [{"rank": 1, "full_name": "owner/alpha", "stars_gained": 101}],
        )

        rows = get_weekly_rankings(self.db_path, "2026-05-11")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["full_name"], "owner/alpha")
        self.assertEqual(rows[0]["stars_gained"], 101)

    def test_appearance_counts_include_current_week_after_save(self) -> None:
        upsert_weekly_rankings(
            self.db_path,
            "2026-05-04",
            "2026-05-10",
            [
                {"rank": 1, "full_name": "owner/alpha", "stars_gained": 100},
                {"rank": 11, "full_name": "owner/hidden", "stars_gained": 10},
            ],
        )
        upsert_weekly_rankings(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            [
                {"rank": 1, "full_name": "owner/beta", "stars_gained": 120},
                {"rank": 2, "full_name": "owner/alpha", "stars_gained": 110},
            ],
        )

        counts = get_appearance_counts(
            self.db_path,
            ["owner/alpha", "owner/beta", "owner/hidden", "owner/missing"],
        )
        rows = get_weekly_rankings(
            self.db_path,
            "2026-05-11",
            include_appearance_counts=True,
        )

        self.assertEqual(counts, {"owner/alpha": 2, "owner/beta": 1})
        self.assertEqual(get_appearance_count(self.db_path, "owner/alpha"), 2)
        self.assertEqual(
            {row["full_name"]: row["appearance_count"] for row in rows},
            {"owner/beta": 1, "owner/alpha": 2},
        )

    def test_email_send_record_is_idempotent_by_default(self) -> None:
        inserted = record_email_sent(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            "reader@example.com",
            message_id="first-message",
            sent_at="2026-05-18T01:00:00+00:00",
        )
        duplicate = record_email_sent(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            "other@example.com",
            message_id="second-message",
            sent_at="2026-05-18T02:00:00+00:00",
        )

        send = get_email_send(self.db_path, "2026-05-11")

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertTrue(has_email_been_sent(self.db_path, "2026-05-11"))
        self.assertEqual(send["recipient"], "reader@example.com")
        self.assertEqual(send["message_id"], "first-message")

    def test_email_send_can_be_explicitly_overwritten_for_force_workflow(self) -> None:
        record_email_sent(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            "reader@example.com",
            message_id="first-message",
        )
        updated = record_email_sent(
            self.db_path,
            "2026-05-11",
            "2026-05-17",
            "other@example.com",
            message_id="forced-message",
            overwrite=True,
        )

        send = get_email_send(self.db_path, "2026-05-11")

        self.assertTrue(updated)
        self.assertEqual(send["recipient"], "other@example.com")
        self.assertEqual(send["message_id"], "forced-message")

    def test_invalid_rows_are_rejected_before_partial_insert(self) -> None:
        with self.assertRaises(DatabaseError):
            upsert_weekly_rankings(
                self.db_path,
                "2026-05-11",
                "2026-05-17",
                [
                    {"rank": 1, "full_name": "owner/alpha", "stars_gained": 100},
                    {"rank": 1, "full_name": "owner/beta", "stars_gained": 90},
                ],
            )

        self.assertEqual(get_weekly_rankings(self.db_path, "2026-05-11"), [])


if __name__ == "__main__":
    unittest.main()
