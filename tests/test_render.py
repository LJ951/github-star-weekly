import unittest

from src.render import build_email_context, render_weekly_email


class RenderTests(unittest.TestCase):
    def test_context_formats_counts_and_appearance_text(self):
        context = build_email_context(
            [
                {
                    "rank": 1,
                    "full_name": "owner/repo",
                    "html_url": "https://github.com/owner/repo",
                    "stars_gained": 1200,
                    "total_stars": 34567,
                    "language": "Rust",
                    "appearance_count": 3,
                    "summary_zh": "中文介绍",
                }
            ],
            week_start="2026-05-11",
            week_end="2026-05-17",
            generated_at="2026-05-18 01:00 UTC",
        )

        repo = context["repositories"][0]
        self.assertEqual(repo["stars_gained_text"], "1,200")
        self.assertEqual(repo["total_stars_text"], "34,567")
        self.assertEqual(repo["appearance_text"], "历史第 3 次上榜")

    def test_rendered_html_contains_required_report_parts(self):
        html = render_weekly_email(
            [
                {
                    "rank": 1,
                    "full_name": "owner/repo",
                    "html_url": "https://github.com/owner/repo",
                    "stars_gained": 1200,
                    "total_stars": 34567,
                    "language": "Rust",
                    "appearance_count": 1,
                    "summary_zh": "这是一个用于测试的中文介绍。",
                }
            ],
            week_start="2026-05-11",
            week_end="2026-05-17",
            generated_at="2026-05-18 01:00 UTC",
        )

        self.assertIn("GitHub 本周 Star 增长最快 Top 10", html)
        self.assertIn("2026-05-11 至 2026-05-17", html)
        self.assertIn("owner/repo", html)
        self.assertIn("首次上榜", html)
        self.assertIn("GH Archive WatchEvent", html)


if __name__ == "__main__":
    unittest.main()
