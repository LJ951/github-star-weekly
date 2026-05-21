import unittest
from unittest.mock import patch

from src.summarize import _clean_summary, build_fallback_summary, summarize_repository


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

    def test_deepseek_env_key_is_used(self):
        repository = {
            "full_name": "owner/repo",
            "description": "A practical automation tool",
            "language": "Python",
        }

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "deepseek-secret"}, clear=False), \
            patch("src.summarize._call_openai", return_value="这是一个足够长的 DeepSeek 中文摘要，用于验证接口配置被正确读取。") as call:
            result = summarize_repository(repository)

        self.assertFalse(result.used_fallback)
        self.assertIn("DeepSeek", result.summary_zh)
        self.assertEqual(call.call_args.kwargs["api_key"], "deepseek-secret")
        self.assertEqual(call.call_args.kwargs["base_url"], "https://api.deepseek.com")
        self.assertEqual(call.call_args.kwargs["model"], "deepseek-v4-flash")

    def test_fallback_handles_sparse_repository_fields(self):
        summary = build_fallback_summary({"full_name": "owner/sparse"})

        self.assertIn("owner/sparse", summary)
        self.assertIn("项目信息有限", summary)
        self.assertIn("未标明主要语言", summary)
        self.assertNotIn("自动摘要服务暂不可用", summary)

    def test_short_non_empty_model_summary_is_accepted(self):
        self.assertEqual(_clean_summary("项目信息有限。"), "项目信息有限。")


if __name__ == "__main__":
    unittest.main()
