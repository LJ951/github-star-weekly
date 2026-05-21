from __future__ import annotations

import base64
import unittest

from src import enrich


class FakeHttpClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[object] = []

    def get_json(self, url: str, headers: object, timeout: int) -> object:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class EnrichTests(unittest.TestCase):
    def test_fetch_repository_details_decodes_and_truncates_readme(self) -> None:
        readme_text = "hello world" * 100
        encoded_readme = base64.b64encode(readme_text.encode("utf-8")).decode("ascii")
        client = FakeHttpClient(
            {
                "https://api.github.com/repos/octocat/hello-world": {
                    "full_name": "octocat/hello-world",
                    "html_url": "https://github.com/octocat/hello-world",
                    "stargazers_count": 42,
                    "language": "Python",
                    "description": "Example repository",
                    "topics": ["demo", "example"],
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "pushed_at": "2026-01-03T00:00:00Z",
                },
                "https://api.github.com/repos/octocat/hello-world/readme": {
                    "encoding": "base64",
                    "content": encoded_readme,
                },
            }
        )

        repo = enrich.fetch_repository_details(
            enrich.RepoInput("octocat/hello-world", 12),
            token="secret-token",
            http_client=client,
            readme_char_limit=20,
        )

        self.assertEqual(repo.full_name, "octocat/hello-world")
        self.assertEqual(repo.stars_gained, 12)
        self.assertEqual(repo.total_stars, 42)
        self.assertEqual(repo.topics, ["demo", "example"])
        self.assertEqual(repo.readme_text, readme_text[:20])
        headers = client.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")

    def test_readme_404_returns_none(self) -> None:
        client = FakeHttpClient(
            {
                "https://api.github.com/repos/octocat/no-readme/readme": enrich.GitHubApiError(
                    404, "GitHub API HTTP 404: Not Found"
                )
            }
        )

        readme = enrich.fetch_readme_text(
            "octocat/no-readme", http_client=client, token=None
        )

        self.assertIsNone(readme)

    def test_non_404_readme_failure_keeps_repository_metadata(self) -> None:
        client = FakeHttpClient(
            {
                "https://api.github.com/repos/octocat/metadata": {
                    "full_name": "octocat/metadata",
                    "html_url": "https://github.com/octocat/metadata",
                    "stargazers_count": 5,
                    "topics": [],
                },
                "https://api.github.com/repos/octocat/metadata/readme": enrich.GitHubApiError(
                    503, "GitHub API HTTP 503"
                ),
            }
        )

        repo = enrich.fetch_repository_details(
            enrich.RepoInput("octocat/metadata", 3),
            http_client=client,
        )

        self.assertEqual(repo.full_name, "octocat/metadata")
        self.assertEqual(repo.total_stars, 5)
        self.assertIsNone(repo.readme_text)

    def test_enrich_repositories_keeps_going_when_one_repo_fails(self) -> None:
        client = FakeHttpClient(
            {
                "https://api.github.com/repos/bad/repo": enrich.GitHubApiError(
                    500, "GitHub API HTTP 500"
                ),
                "https://api.github.com/repos/good/repo": {
                    "full_name": "good/repo",
                    "html_url": "https://github.com/good/repo",
                    "stargazers_count": 7,
                    "topics": [],
                },
                "https://api.github.com/repos/good/repo/readme": enrich.GitHubApiError(
                    404, "GitHub API HTTP 404: Not Found"
                ),
            }
        )

        repos = enrich.enrich_repositories(
            [
                enrich.RepoInput("bad/repo", 10),
                {"full_name": "good/repo", "stars_gained": 9},
            ],
            token="token-value",
            http_client=client,
        )

        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0].full_name, "bad/repo")
        self.assertEqual(repos[0].html_url, "https://github.com/bad/repo")
        self.assertIn("GitHub enrichment failed", repos[0].error or "")
        self.assertEqual(repos[1].full_name, "good/repo")
        self.assertEqual(repos[1].total_stars, 7)

    def test_enrich_repositories_reads_config_defaults(self) -> None:
        client = FakeHttpClient(
            {
                "https://api.github.com/repos/good/configured": {
                    "full_name": "good/configured",
                    "html_url": "https://github.com/good/configured",
                    "stargazers_count": 7,
                    "topics": [],
                },
                "https://api.github.com/repos/good/configured/readme": enrich.GitHubApiError(
                    404, "GitHub API HTTP 404: Not Found"
                ),
            }
        )

        enrich.enrich_repositories(
            [enrich.RepoInput("good/configured", 9)],
            config={
                "github_token": "configured-token",
                "github_api_timeout_seconds": 3,
                "readme_max_chars": 10,
            },
            http_client=client,
        )

        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer configured-token")
        self.assertEqual(client.calls[0]["timeout"], 3)

    def test_invalid_full_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            enrich.validate_full_name("not-a-full-name")

    def test_enriched_repo_behaves_like_mapping_for_downstream_modules(self) -> None:
        repo = enrich.EnrichedRepo(
            full_name="octocat/hello-world",
            html_url="https://github.com/octocat/hello-world",
            stars_gained=1,
            topics=["demo"],
        )

        as_dict = dict(repo)

        self.assertEqual(repo["full_name"], "octocat/hello-world")
        self.assertEqual(as_dict["topics"], ["demo"])
        self.assertIn("readme_text", as_dict)


if __name__ == "__main__":
    unittest.main()
