import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from providers import RepositoryEvent

DATA_ROOT = Path(tempfile.mkdtemp(prefix="release-monitor-tests-"))


def install_stubs():
    for name in list(sys.modules):
        if name == "main" or name.startswith("astrbot"):
            sys.modules.pop(name, None)
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    paths = types.ModuleType("astrbot.core.utils.astrbot_path")

    class Config(dict):
        pass

    class Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class Event:
        def is_admin(self):
            return True

        def plain_result(self, value):
            return value

    class Filter:
        @staticmethod
        def command(_name):
            return lambda func: func

    class Star:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_release_monitor"

    def register(*args, **kwargs):
        return lambda cls: cls

    api.AstrBotConfig = Config
    api.logger = Logger()
    event.AstrMessageEvent = Event
    event.filter = Filter()
    star.Context = object
    star.Star = Star
    star.register = register
    paths.get_astrbot_data_path = lambda: DATA_ROOT
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
            "astrbot.core.utils.astrbot_path": paths,
        }
    )


install_stubs()
main = importlib.import_module("main")


class ReleaseMonitorTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, **config):
        return main.ReleaseMonitorPlugin(object(), config)

    def test_parse_repositories_and_legacy_config(self):
        result = main.ReleaseMonitorPlugin.parse_repositories(
            [
                {"repo": "owner/repo", "include_prereleases": True},
                {
                    "repo": "https://github.com/owner/other/releases",
                    "include_prereleases": False,
                },
            ]
        )
        self.assertEqual(result[0]["include_prereleases"], True)
        plugin = self.make_plugin(
            repositories=[
                {
                    "platform": "gitlab",
                    "repository": "group/project",
                    "monitor_commit": True,
                }
            ]
        )
        self.assertEqual(plugin.repositories[0].platform, "gitlab")
        self.assertTrue(plugin.repositories[0].monitor_commit)
        self.assertFalse(plugin.repositories[0].monitor_release)

    def test_format_event_message_is_compact(self):
        title, message = main.ReleaseMonitorPlugin.format_event_message(
            main.RepositoryTarget("github", "owner/repo", "main", True),
            RepositoryEvent(
                "commit",
                "sha",
                "Fix bug",
                "abcdef123456",
                "alice",
                "2026-01-01T00:00:00Z",
                "https://github.com/owner/repo/commit/sha",
                "main",
            ),
        )
        self.assertEqual(title, "GitHub Commit Update")
        self.assertIn("版本/SHA: abcdef123456", message)
        self.assertIn("作者: alice", message)
        self.assertIn("链接: https://github.com/owner/repo/commit/sha", message)
        self.assertNotIn("完整", message)

    async def test_each_event_is_notified_once_and_state_persisted(self):
        plugin = self.make_plugin(
            repositories=[
                {
                    "platform": "github",
                    "repository": "owner/repo",
                    "monitor_commit": True,
                    "monitor_release": True,
                    "monitor_prerelease": True,
                }
            ],
            gotify_channels=[],
        )
        events = {
            "commit": RepositoryEvent(
                "commit", "sha1", "Commit", "sha1", "a", "", "url", "main"
            ),
            "release": RepositoryEvent(
                "release", "r1", "Release", "v1", "a", "", "url"
            ),
            "prerelease": RepositoryEvent(
                "prerelease", "p1", "Pre", "v2-rc.1", "a", "", "url"
            ),
        }
        provider = plugin.providers["github"]
        provider.fetch_latest_commit = lambda target: events["commit"]
        provider.fetch_latest_release = lambda target, prerelease: events[
            "prerelease" if prerelease else "release"
        ]
        plugin.notify_event = AsyncMock(return_value=0)
        self.assertEqual(len(await plugin.check_events()), 0)
        events["commit"] = RepositoryEvent(
            "commit", "sha2", "Commit 2", "sha2", "a", "", "url", "main"
        )
        events["release"] = RepositoryEvent(
            "release", "r2", "Release 2", "v2", "a", "", "url"
        )
        events["prerelease"] = RepositoryEvent(
            "prerelease", "p2", "Pre 2", "v3-rc.1", "a", "", "url"
        )
        self.assertEqual(len(await plugin.check_events()), 3)
        self.assertEqual(len(await plugin.check_events()), 0)
        self.assertEqual(plugin.notify_event.await_count, 3)
        self.assertIn("github:owner/repo", plugin.state)
        restored = self.make_plugin(repositories=["owner/repo"])
        await restored.load_state()
        self.assertEqual(restored.state["github:owner/repo"]["release"]["key"], "r2")

    async def test_first_run_can_notify_and_old_state_is_migrated(self):
        plugin = self.make_plugin(
            repositories=[{"repository": "owner/repo", "monitor_release": True}],
            notify_on_first_run=True,
        )
        event = RepositoryEvent("release", "r1", "Release", "v1", "a", "", "url")
        plugin.providers["github"].fetch_latest_release = lambda target, prerelease: (
            event
        )
        plugin.notify_event = AsyncMock(return_value=1)
        self.assertEqual(len(await plugin.check_events()), 1)
        plugin.notify_event.assert_awaited_once()

        old_path = plugin.get_state_path()
        plugin.write_json(
            old_path,
            {
                "owner/old": {
                    "release_key": "old-key",
                    "tag_name": "v0",
                    "html_url": "old-url",
                }
            },
        )
        migrated = self.make_plugin()
        await migrated.load_state()
        self.assertEqual(
            migrated.state["github:owner/old"]["release"]["key"], "old-key"
        )

    async def test_initialize_schedules_initial_check_in_background(self):
        plugin = self.make_plugin(
            repositories=[{"repository": "owner/repo", "monitor_commit": True}]
        )
        plugin.check_releases = AsyncMock()
        await plugin.initialize()
        plugin.check_releases.assert_not_awaited()
        await asyncio.sleep(0)
        plugin.check_releases.assert_awaited_once()
        await plugin.terminate()

    async def test_commands_show_targets_and_counts(self):
        plugin = self.make_plugin(
            repositories=[
                {
                    "platform": "github",
                    "repository": "owner/repo",
                    "branch": "main",
                    "monitor_commit": True,
                },
                {
                    "platform": "gitlab",
                    "repository": "group/project",
                    "monitor_release": True,
                },
            ]
        )
        plugin.state["github:owner/repo"] = {"commit": {"version": "abcdef123456"}}
        plugin.last_check_at = "2026-01-01T00:00:00+00:00"
        listed = await anext(
            plugin.release_list(
                types.SimpleNamespace(is_admin=lambda: True, plain_result=lambda x: x)
            )
        )
        status = await anext(
            plugin.release_status(
                types.SimpleNamespace(is_admin=lambda: True, plain_result=lambda x: x)
            )
        )
        self.assertIn("github:owner/repo", listed)
        self.assertIn("[main]", listed)
        self.assertIn("Commit", listed)
        self.assertIn("abcdef123456", listed)
        self.assertIn("GitLab 仓库: 1", status)
        self.assertIn("Commit 监控: 1", status)
        self.assertIn("仓库数量: 2", status)
        self.assertIn("GitHub 仓库: 1", status)
        self.assertIn("Release 监控: 1", status)
        self.assertIn("Pre-release 监控: 0", status)
        self.assertIn("Gotify 渠道: 0", status)
        self.assertIn("最后检查: 2026-01-01T00:00:00+00:00", status)

    async def test_commands_reject_non_admin(self):
        plugin = self.make_plugin()
        event = types.SimpleNamespace(is_admin=lambda: False, plain_result=lambda x: x)
        self.assertEqual(await anext(plugin.release_check(event)), "仅管理员可用")
        self.assertEqual(await anext(plugin.release_list(event)), "仅管理员可用")
        self.assertEqual(await anext(plugin.release_status(event)), "仅管理员可用")

    async def test_no_enabled_events_make_no_provider_request(self):
        plugin = self.make_plugin(repositories=[{"repository": "owner/repo"}])
        provider = plugin.providers["github"]
        provider.fetch_latest_commit = AsyncMock()
        provider.fetch_latest_release = AsyncMock()
        self.assertEqual(await plugin.check_events(), [])
        provider.fetch_latest_commit.assert_not_awaited()
        provider.fetch_latest_release.assert_not_awaited()

    async def test_initialize_does_not_start_when_all_events_disabled(self):
        plugin = self.make_plugin(repositories=[{"repository": "owner/repo"}])
        await plugin.initialize()
        self.assertIsNone(plugin.monitor_task)

    async def test_initialize_does_not_start_without_repositories(self):
        plugin = self.make_plugin()
        await plugin.initialize()
        self.assertIsNone(plugin.monitor_task)
        self.assertEqual(await plugin.check_events(), [])

    async def test_only_prerelease_is_checked(self):
        plugin = self.make_plugin(
            repositories=[{"repository": "owner/repo", "monitor_prerelease": True}]
        )
        stable = RepositoryEvent("release", "r", "Release", "v1", "a", "", "url")
        pre = RepositoryEvent("prerelease", "p", "Pre", "v2-rc.1", "a", "", "url")
        calls = []
        plugin.providers["github"].fetch_latest_release = lambda target, prerelease: (
            calls.append(prerelease) or (pre if prerelease else stable)
        )
        self.assertEqual(await plugin.check_events(), [])
        self.assertEqual(calls, [True])

    async def test_event_failure_does_not_stop_other_event_on_same_target(self):
        plugin = self.make_plugin(
            repositories=[
                {
                    "repository": "owner/repo",
                    "monitor_commit": True,
                    "monitor_release": True,
                }
            ]
        )
        plugin.providers["github"].fetch_latest_commit = lambda target: (
            _ for _ in ()
        ).throw(RuntimeError("failed"))
        plugin.providers["github"].fetch_latest_release = lambda target, prerelease: (
            RepositoryEvent("release", "r", "Release", "v1", "a", "", "url")
        )
        self.assertEqual(await plugin.check_events(), [])
        self.assertIn("release", plugin.state["github:owner/repo"])

    async def test_missing_release_does_not_create_state(self):
        plugin = self.make_plugin(
            repositories=[
                {
                    "repository": "owner/repo",
                    "monitor_release": True,
                    "monitor_prerelease": True,
                }
            ]
        )
        plugin.providers["github"].fetch_latest_release = lambda target, prerelease: (
            None
        )
        self.assertEqual(await plugin.check_events(), [])
        self.assertEqual(plugin.state, {})

    async def test_failed_event_does_not_stop_other_targets(self):
        plugin = self.make_plugin(
            repositories=[
                {
                    "platform": "github",
                    "repository": "owner/disabled",
                    "monitor_commit": True,
                },
                {
                    "platform": "github",
                    "repository": "owner/enabled",
                    "monitor_commit": True,
                },
            ]
        )
        good = RepositoryEvent("commit", "sha", "Commit", "sha", "a", "", "url", "main")
        plugin.providers["github"].fetch_latest_commit = lambda target: (
            (_ for _ in ()).throw(RuntimeError("failed"))
            if target.repository == "owner/disabled"
            else good
        )
        self.assertEqual(await plugin.check_events(), [])
        self.assertIn("github:owner/enabled", plugin.state)

    def test_write_json_uses_expected_path(self):
        plugin = self.make_plugin()
        path = plugin.get_state_path()
        plugin.write_json(path, {"github:owner/repo": {"release": {"key": "1"}}})
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8").count('"key"'), 1)
        self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
