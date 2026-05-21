"""End-to-end smoke test for src.main using in-process fake modules.

Run with:
    python tests/smoke_main.py
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def install_fake_modules() -> dict[str, int]:
    calls = {
        "collect": 0,
        "enrich": 0,
        "summarize": 0,
        "render": 0,
        "send": 0,
        "record": 0,
    }

    config = types.ModuleType("src.config")

    def load_config() -> dict[str, str]:
        return {"email_to": "weekly@example.com"}

    config.load_config = load_config

    collect = types.ModuleType("src.collect")

    def collect_top_repositories(week_start, week_end, limit, config):
        calls["collect"] += 1
        assert str(week_start) == "2026-05-11"
        assert str(week_end) == "2026-05-17"
        assert limit == 10
        return [
            {"full_name": "owner/project-a", "stars_gained": 123},
            {"full_name": "owner/project-b", "stars_gained": 88},
        ]

    collect.collect_top_repositories = collect_top_repositories

    enrich = types.ModuleType("src.enrich")

    def enrich_repositories(repositories, config):
        calls["enrich"] += 1
        enriched = []
        for repo in repositories:
            updated = dict(repo)
            updated.update(
                {
                    "description": f"{repo['full_name']} description",
                    "language": "Python",
                    "stargazers_count": 1000,
                    "topics": ["automation", "github"],
                }
            )
            enriched.append(updated)
        return enriched

    enrich.enrich_repositories = enrich_repositories

    summarize = types.ModuleType("src.summarize")

    def summarize_repositories(repositories, config):
        calls["summarize"] += 1
        return [
            {**repo, "summary_zh": f"{repo['full_name']} 的中文介绍。"}
            for repo in repositories
        ]

    summarize.summarize_repositories = summarize_repositories

    render = types.ModuleType("src.render")

    def render_weekly_email(repositories, week_start, week_end, generated_at, config):
        calls["render"] += 1
        assert all(repo["appearance_count"] == 1 for repo in repositories)
        return "<html><body>weekly report</body></html>"

    render.render_weekly_email = render_weekly_email

    emailer = types.ModuleType("src.emailer")

    def send_weekly_email(html, subject, idempotency_key, force, config):
        calls["send"] += 1
        assert "Top 10" in subject
        assert idempotency_key.startswith("github-star-weekly:2026-05-11")
        if calls["send"] == 1:
            assert idempotency_key == "github-star-weekly:2026-05-11"
            assert force is False
        else:
            assert ":force:" in idempotency_key
            assert force is True
        assert html.startswith("<html>")
        return {"id": "msg_smoke"}

    emailer.send_weekly_email = send_weekly_email

    db = types.ModuleType("src.db")

    def connect(db_path):
        return sqlite3.connect(db_path)

    def init_db(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_rankings (
              week_start TEXT NOT NULL,
              week_end TEXT NOT NULL,
              rank INTEGER NOT NULL,
              full_name TEXT NOT NULL,
              html_url TEXT NOT NULL,
              stars_gained INTEGER NOT NULL,
              total_stars INTEGER,
              language TEXT,
              description TEXT,
              topics TEXT,
              summary_zh TEXT,
              PRIMARY KEY (week_start, full_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_sends (
              week_start TEXT PRIMARY KEY,
              week_end TEXT NOT NULL,
              recipient TEXT NOT NULL,
              message_id TEXT
            )
            """
        )
        conn.commit()

    def has_email_sent(conn, week_start):
        return (
            conn.execute(
                "SELECT 1 FROM email_sends WHERE week_start = ?",
                (week_start,),
            ).fetchone()
            is not None
        )

    def upsert_weekly_rankings(conn, week_start, week_end, repositories):
        for repo in repositories:
            conn.execute(
                """
                INSERT INTO weekly_rankings (
                  week_start, week_end, rank, full_name, html_url, stars_gained,
                  total_stars, language, description, topics, summary_zh
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(week_start, full_name) DO UPDATE SET
                  rank = excluded.rank,
                  html_url = excluded.html_url,
                  stars_gained = excluded.stars_gained,
                  total_stars = excluded.total_stars,
                  language = excluded.language,
                  description = excluded.description,
                  topics = excluded.topics,
                  summary_zh = excluded.summary_zh
                """,
                (
                    week_start,
                    week_end,
                    repo["rank"],
                    repo["full_name"],
                    repo["html_url"],
                    repo["stars_gained"],
                    repo.get("total_stars"),
                    repo.get("language"),
                    repo.get("description"),
                    ",".join(repo.get("topics", [])),
                    repo.get("summary_zh"),
                ),
            )
        conn.commit()

    def get_repo_appearance_count(conn, full_name):
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_rankings WHERE full_name = ? AND rank <= 10",
            (full_name,),
        ).fetchone()[0]

    def record_email_sent(conn, week_start, week_end, recipient, message_id):
        calls["record"] += 1
        conn.execute(
            """
            INSERT INTO email_sends (week_start, week_end, recipient, message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(week_start) DO UPDATE SET
              week_end = excluded.week_end,
              recipient = excluded.recipient,
              message_id = excluded.message_id
            """,
            (week_start, week_end, recipient, message_id),
        )
        conn.commit()

    db.connect = connect
    db.init_db = init_db
    db.has_email_sent = has_email_sent
    db.upsert_weekly_rankings = upsert_weekly_rankings
    db.get_repo_appearance_count = get_repo_appearance_count
    db.record_email_sent = record_email_sent

    for module in (config, collect, enrich, summarize, render, emailer, db):
        sys.modules[module.__name__] = module

    return calls


def main() -> int:
    calls = install_fake_modules()
    main_module = importlib.import_module("src.main")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "rankings.sqlite"

        first = main_module.main(
            [
                "--week-start",
                "2026-05-11",
                "--db-path",
                str(db_path),
                "--log-level",
                "ERROR",
            ]
        )
        assert first == 0
        assert calls["send"] == 1
        assert calls["record"] == 1

        second = main_module.main(
            [
                "--week-start",
                "2026-05-11",
                "--db-path",
                str(db_path),
                "--log-level",
                "ERROR",
            ]
        )
        assert second == 0
        assert calls["send"] == 1, "second run must not resend by default"

        forced = main_module.main(
            [
                "--week-start",
                "2026-05-11",
                "--db-path",
                str(db_path),
                "--force",
                "--log-level",
                "ERROR",
            ]
        )
        assert forced == 0
        assert calls["send"] == 2, "--force should resend"

        conn = sqlite3.connect(db_path)
        try:
            ranking_count = conn.execute("SELECT COUNT(*) FROM weekly_rankings").fetchone()[0]
            send_count = conn.execute("SELECT COUNT(*) FROM email_sends").fetchone()[0]
        finally:
            conn.close()
        assert ranking_count == 2
        assert send_count == 1

    print("smoke_main passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
