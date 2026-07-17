import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_parse_repositories(self):
        result = main.ReleaseMonitorPlugin.parse_repositories(
            [
                {"repo": "owner/repo", "include_prereleases": True},
                "https://github.com/owner/other/releases",
                "bad",
                "owner/repo",
            ]
        )
        self.assertEqual(
            result,
            [
                {"repo": "owner/repo", "include_prereleases": True},
                {"repo": "owner/other", "include_prereleases": False},
            ],
        )

    def test_parse_repositories_supports_legacy_global_default(self):
        result = main.ReleaseMonitorPlugin.parse_repositories(
            ["owner/repo"], default_include_prereleases=True
        )
        self.assertEqual(
            result, [{"repo": "owner/repo", "include_prereleases": True}]
        )

    def test_parse_gotify_channels(self):
        result = main.ReleaseMonitorPlugin.parse_gotify_channels(
            [{"name": "main", "url": "https://gotify.example.com/", "token": "abc"}]
        )
        self.assertEqual(result[0]["url"], "https://gotify.example.com")
        self.assertEqual(result[0]["priority"], 5)

    def test_format_release_message_is_compact(self):
        title, message = main.ReleaseMonitorPlugin.format_release_message(
            "roflmuffin/CounterStrikeSharp",
            {
                "tag_name": "v1.0.371",
                "published_at": "2026-07-10T09:48:21Z",
                "body": "This long release body should not be included.",
                "html_url": "https://github.com/roflmuffin/CounterStrikeSharp/releases/tag/v1.0.371",
            },
        )
        self.assertEqual(title, "GitHub Release Update")
        self.assertEqual(
            message,
            "仓库: roflmuffin/CounterStrikeSharp\n"
            "版本: v1.0.371\n"
            "时间: 2026-07-10 09:48:21\n"
            "链接: https://github.com/roflmuffin/CounterStrikeSharp/releases/tag/v1.0.371",
        )

    async def test_state_is_persisted_and_new_release_is_notified_once(self):
        plugin = self.make_plugin(
            repositories=[{"repo": "owner/repo", "include_prereleases": False}],
            gotify_channels=[{"url": "https://gotify.example.com", "token": "abc"}],
            notify_on_first_run=False,
        )
        release = {
            "id": 1,
            "tag_name": "v1.0.0",
            "name": "First",
            "published_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/releases/tag/v1.0.0",
        }
        with patch.object(plugin, "fetch_latest_release", return_value=release), patch.object(
            plugin, "notify", return_value=1
        ) as notify:
            self.assertEqual(await plugin.check_releases(), [])
            release["id"] = 2
            release["tag_name"] = "v2.0.0"
            self.assertEqual(len(await plugin.check_releases()), 1)
            self.assertEqual(len(await plugin.check_releases()), 0)
            notify.assert_awaited_once()

        restored = self.make_plugin(repositories=["owner/repo"])
        await restored.load_state()
        self.assertEqual(restored.state["owner/repo"]["tag_name"], "v2.0.0")

    def test_write_json_uses_expected_path(self):
        plugin = self.make_plugin()
        path = plugin.get_state_path()
        plugin.write_json(path, {"owner/repo": {"release_key": "1"}})
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8").count("release_key"), 1)


if __name__ == "__main__":
    unittest.main()
