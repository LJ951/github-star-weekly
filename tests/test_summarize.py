import unittest

from src.summarize import build_fallback_summary, summarize_repository


class SummarizeTests(unittest.TestCase):
    def test_missing_api_key_uses_fallback_summary(self):
        repository = {
            "full_name": "owner/repo",
            "description": "A practical automation tool",
            "language": "Python",
            "topics": ["github", "automation"],
            "stars_gained": 123,
            "total_stars": 4567,
        }

        result = summarize_repository(repository, api_key="")

        self.assertTrue(result.used_fallback)
        self.assertIn("owner/repo", result.summary_zh)
        self.assertIn("Python", result.summary_zh)
        self.assertIn("123", result.summary_zh)
        self.assertIn("4,567", result.summary_zh)

    def test_fallback_handles_sparse_repository_fields(self):
        summary = build_fallback_summary({"full_name": "owner/sparse"})

        self.assertIn("owner/sparse", summary)
        self.assertIn("项目信息有限", summary)
        self.assertIn("未标明主要语言", summary)


if __name__ == "__main__":
    unittest.main()
