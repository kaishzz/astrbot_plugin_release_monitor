import unittest
from unittest.mock import Mock, patch

from providers import (
    GitHubProvider,
    GitLabProvider,
    RepositoryTarget,
    is_gitlab_prerelease_tag,
    parse_targets,
)


def response(payload, status=200):
    item = Mock()
    item.status_code = status
    item.json.return_value = payload
    item.raise_for_status.return_value = None
    return item


class ProviderTests(unittest.TestCase):
    def test_parse_targets_and_legacy_mapping(self):
        targets = parse_targets(
            [
                {"repository": "owner/repo", "monitor_commit": True},
                {
                    "platform": "gitlab",
                    "repository": "https://gitlab.com/group/project",
                    "monitor_release": True,
                },
                {"repo": "legacy/repo", "include_prereleases": True},
                {"repo": "legacy/stable"},
                {"repo": "legacy/string-false", "include_prereleases": "false"},
            ]
        )
        self.assertEqual(targets[1].target_key, "gitlab:group/project")
        self.assertFalse(targets[0].monitor_release)
        self.assertTrue(targets[2].monitor_release)
        self.assertTrue(targets[2].monitor_prerelease)
        self.assertTrue(targets[3].monitor_release)
        self.assertFalse(targets[3].monitor_prerelease)
        self.assertTrue(targets[4].monitor_release)
        self.assertFalse(targets[4].monitor_prerelease)

    def test_invalid_and_duplicate_targets_are_ignored(self):
        targets = parse_targets(
            [
                {"repository": "owner/repo", "monitor_commit": True},
                {"repository": "owner/repo", "monitor_release": True},
                {
                    "platform": "bitbucket",
                    "repository": "owner/other",
                    "monitor_commit": True,
                },
                {"platform": "github", "repository": "invalid", "monitor_commit": True},
            ]
        )
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].monitor_commit)

    def test_empty_repository_config_is_ignored(self):
        self.assertEqual(parse_targets([{}, {"repository": ""}]), [])

    def test_gitlab_prerelease_tag_rules(self):
        self.assertTrue(is_gitlab_prerelease_tag("v1.2.3-rc.1"))
        self.assertTrue(is_gitlab_prerelease_tag("1.0.0-beta"))
        self.assertTrue(is_gitlab_prerelease_tag("v2.0.0-nightly"))
        self.assertFalse(is_gitlab_prerelease_tag("v1.2.3"))
        self.assertFalse(is_gitlab_prerelease_tag("v1.2.3.rc"))
        self.assertFalse(is_gitlab_prerelease_tag("v1.2.3-rc-1"))

    @patch("providers.requests.get")
    def test_default_branch_is_resolved_from_platform(self, get):
        get.side_effect = [
            response({"default_branch": "trunk"}),
            response([{"sha": "sha", "commit": {"message": "Fix"}, "html_url": "url"}]),
        ]
        event = GitHubProvider().fetch_latest_commit(
            RepositoryTarget("github", "owner/repo")
        )
        self.assertEqual(event.branch, "trunk")
        self.assertEqual(
            get.call_args_list[1].kwargs["params"], {"per_page": 1, "sha": "trunk"}
        )

    @patch("providers.requests.get")
    def test_github_commit_and_release_conversion(self, get):
        provider = GitHubProvider("token")
        target = RepositoryTarget("github", "owner/repo", "main")
        get.return_value = response(
            [
                {
                    "sha": "abcdef1234567890",
                    "html_url": "commit-url",
                    "commit": {
                        "message": "Fix\nbody",
                        "author": {"name": "Alice"},
                        "committer": {"date": "date"},
                    },
                    "author": {"login": "alice"},
                }
            ]
        )
        commit = provider.fetch_latest_commit(target)
        self.assertEqual(commit.key, "abcdef1234567890")
        self.assertEqual(commit.title, "Commit")
        self.assertEqual(get.call_args.kwargs["params"], {"per_page": 1, "sha": "main"})
        self.assertEqual(
            get.call_args_list[0].args[0],
            "https://api.github.com/repos/owner/repo/commits",
        )
        get.return_value = response(
            [
                {
                    "id": 3,
                    "tag_name": "draft",
                    "prerelease": False,
                    "draft": True,
                    "published_at": "2026-04-01",
                    "html_url": "draft",
                },
                {
                    "id": 1,
                    "tag_name": "v2",
                    "prerelease": True,
                    "draft": False,
                    "published_at": "2026-02-01",
                    "html_url": "pre",
                },
                {
                    "id": 2,
                    "tag_name": "v1",
                    "prerelease": False,
                    "draft": False,
                    "published_at": "2026-01-01",
                    "html_url": "stable",
                },
            ]
        )
        self.assertEqual(provider.fetch_latest_release(target, False).version, "v1")
        self.assertEqual(provider.fetch_latest_release(target, True).version, "v2")
        self.assertIn(
            "api.github.com/repos/owner/repo/releases", get.call_args_list[1].args[0]
        )

    @patch("providers.requests.get")
    def test_gitlab_commit_release_and_upcoming_filter(self, get):
        provider = GitLabProvider()
        target = RepositoryTarget("gitlab", "group/project", "main")
        get.return_value = response(
            [
                {
                    "id": "abcdef1234567890",
                    "short_id": "abcdef123456",
                    "title": "Fix",
                    "author_name": "Alice",
                    "committed_date": "date",
                    "web_url": "commit-url",
                }
            ]
        )
        commit = provider.fetch_latest_commit(target)
        self.assertEqual(commit.version, "abcdef123456")
        self.assertEqual(
            get.call_args.kwargs["params"], {"per_page": 1, "ref_name": "main"}
        )
        get.return_value = response(
            [
                {
                    "tag_name": "v3.0.0-rc.1",
                    "released_at": "2026-03-01",
                    "_links": {"self": "pre"},
                },
                {
                    "tag_name": "v4.0.0",
                    "released_at": "2026-02-01",
                    "_links": {"self": "stable"},
                },
                {
                    "tag_name": "v5.0.0-rc.1",
                    "released_at": "2026-04-01",
                    "upcoming_release": True,
                    "_links": {"self": "upcoming"},
                },
            ]
        )
        self.assertEqual(
            provider.fetch_latest_release(target, True).version, "v3.0.0-rc.1"
        )
        self.assertEqual(provider.fetch_latest_release(target, False).version, "v4.0.0")

    @patch("providers.requests.get")
    def test_gitlab_default_branch_and_encoded_project_path(self, get):
        get.side_effect = [
            response({"default_branch": "trunk"}),
            response(
                [{"id": "sha", "short_id": "sha", "title": "Fix", "web_url": "url"}]
            ),
        ]
        provider = GitLabProvider()
        event = provider.fetch_latest_commit(
            RepositoryTarget("gitlab", "group/sub/project")
        )
        self.assertEqual(event.branch, "trunk")
        self.assertIn("projects/group%2Fsub%2Fproject", get.call_args_list[0].args[0])

    @patch("providers.requests.get")
    def test_http_errors_and_404(self, get):
        provider = GitHubProvider()
        target = RepositoryTarget("github", "owner/repo")
        get.return_value = response(None, 404)
        self.assertIsNone(provider.fetch_latest_commit(target))
        get.return_value = response(None, 429)
        get.return_value.raise_for_status.side_effect = RuntimeError("rate limited")
        with self.assertRaises(RuntimeError):
            provider.fetch_latest_commit(target)

    @patch("providers.requests.get")
    def test_empty_commit_response_returns_none(self, get):
        get.return_value = response([])
        self.assertIsNone(
            GitHubProvider().fetch_latest_commit(
                RepositoryTarget("github", "owner/repo", "main")
            )
        )

    @patch("providers.requests.get")
    def test_gitlab_404_returns_none(self, get):
        get.return_value = response(None, 404)
        self.assertIsNone(
            GitLabProvider().fetch_latest_commit(
                RepositoryTarget("gitlab", "group/project", "main")
            )
        )


if __name__ == "__main__":
    unittest.main()
