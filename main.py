import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from providers import (
        GitHubProvider,
        GitLabProvider,
        RepositoryEvent,
        RepositoryTarget,
        normalize_text,
        parse_bool,
        parse_targets,
    )
except ModuleNotFoundError as exc:
    if exc.name != "providers":
        raise
    provider_path = Path(__file__).with_name("providers.py")
    provider_spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_release_monitor_providers", provider_path
    )
    if provider_spec is None or provider_spec.loader is None:
        raise ImportError(f"无法加载平台适配器: {provider_path}") from exc
    provider_module = importlib.util.module_from_spec(provider_spec)
    sys.modules[provider_spec.name] = provider_module
    provider_spec.loader.exec_module(provider_module)
    GitHubProvider = provider_module.GitHubProvider
    GitLabProvider = provider_module.GitLabProvider
    RepositoryEvent = provider_module.RepositoryEvent
    RepositoryTarget = provider_module.RepositoryTarget
    normalize_text = provider_module.normalize_text
    parse_bool = provider_module.parse_bool
    parse_targets = provider_module.parse_targets

EVENT_DISPLAY_NAMES = {
    "commit": "Commit",
    "release": "Release",
    "prerelease": "Pre-release",
}


@register(
    "astrbot_plugin_release_monitor",
    "kaish",
    "监控 GitHub 仓库 Release, 并通过 Gotify 通知",
    "1.1",
)
class ReleaseMonitorPlugin(Star):
    STATE_FILENAME = "release_state.json"
    DEFAULT_INTERVAL_MINUTES = 30

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.repositories: List[RepositoryTarget] = parse_targets(
            config.get("repositories", [])
        )
        self.github_token = normalize_text(config.get("github_token"))
        self.interval_minutes = self.read_int(
            "check_interval_minutes", self.DEFAULT_INTERVAL_MINUTES, minimum=1
        )
        self.notify_on_first_run = self.read_bool("notify_on_first_run", False)
        self.gotify_channels = self.parse_gotify_channels(
            config.get("gotify_channels", [])
        )
        self.state: Dict[str, Dict[str, Any]] = {}
        self.state_lock = asyncio.Lock()
        self.check_lock = asyncio.Lock()
        self.monitor_task: Optional[asyncio.Task] = None
        self.last_check_at: Optional[str] = None
        self.last_notification_counts: Dict[str, int] = {}
        self.last_change_records: List[
            Tuple[RepositoryTarget, RepositoryEvent, int]
        ] = []
        self.providers = {
            "github": GitHubProvider(self.github_token),
            "gitlab": GitLabProvider(),
        }

    @classmethod
    def parse_repositories(cls, value: Any) -> List[Dict[str, Any]]:
        return [
            {
                "repo": target.repository,
                "include_prereleases": target.monitor_prerelease,
            }
            for target in parse_targets(value)
        ]

    @classmethod
    def parse_gotify_channels(cls, value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []

        channels = []
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                continue
            url = normalize_text(raw.get("url")).rstrip("/")
            token = normalize_text(raw.get("token"))
            if not url or not token or not url.startswith(("http://", "https://")):
                logger.warning(f"Gotify 渠道 {index} 配置不完整, 已跳过")
                continue
            try:
                priority = int(raw.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            channels.append(
                {
                    "name": normalize_text(raw.get("name")) or f"Gotify {index}",
                    "url": url,
                    "token": token,
                    "priority": max(0, priority),
                }
            )
        return channels

    def read_int(self, key: str, default: int, minimum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    def read_bool(self, key: str, default: bool) -> bool:
        return parse_bool(self.config.get(key, default), default)

    def get_state_path(self) -> Path:
        plugin_name = getattr(self, "name", "astrbot_plugin_release_monitor")
        return (
            Path(os.fspath(get_astrbot_data_path()))
            / "plugin_data"
            / plugin_name
            / self.STATE_FILENAME
        )

    @staticmethod
    def read_json(path: Path) -> Dict[str, Dict[str, Any]]:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def write_json(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    async def load_state(self) -> None:
        path = self.get_state_path()
        if not path.exists():
            return
        try:
            data = await asyncio.to_thread(self.read_json, path)
            migrated: Dict[str, Dict[str, Any]] = {}
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                if "release_key" in value:
                    target_key = key if ":" in key else f"github:{key}"
                    migrated[target_key] = {
                        "release": {
                            "key": value.get("release_key"),
                            "tag_name": value.get("tag_name"),
                            "checked_at": value.get("checked_at"),
                        }
                    }
                else:
                    migrated[key] = value
            async with self.state_lock:
                self.state = migrated
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"读取 Release 持久化文件失败: {path}, {exc}")

    async def save_state(self) -> None:
        async with self.state_lock:
            snapshot = dict(self.state)
        await asyncio.to_thread(self.write_json, self.get_state_path(), snapshot)

    @staticmethod
    def format_event_message(
        target: RepositoryTarget, event: RepositoryEvent
    ) -> Tuple[str, str]:
        event_name = EVENT_DISPLAY_NAMES[event.event_type]
        platform_name = {"github": "GitHub", "gitlab": "GitLab"}[target.platform]
        title = f"{platform_name} {event_name} Update"
        lines = [
            f"平台: {platform_name}",
            f"仓库: {target.repository}",
            f"类型: {event_name}",
        ]
        if event.branch:
            lines.append(f"分支: {event.branch}")
        if event.version:
            lines.append(f"版本/SHA: {event.version}")
        if event.title:
            lines.append(f"标题: {event.title}")
        lines.append(f"作者: {event.author or '未知'}")
        lines.extend(
            [f"时间: {event.published_at or '未知时间'}", f"链接: {event.url}"]
        )
        return title, "\n".join(lines)

    def send_gotify(self, channel: Dict[str, Any], title: str, message: str) -> None:
        endpoint = f"{channel['url']}/message?token={quote(channel['token'], safe='')}"
        response = requests.post(
            endpoint,
            json={"title": title, "message": message, "priority": channel["priority"]},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        response.raise_for_status()

    async def notify_event(
        self, target: RepositoryTarget, event: RepositoryEvent
    ) -> int:
        title, message = self.format_event_message(target, event)
        success_count = 0
        for channel in self.gotify_channels:
            try:
                await asyncio.to_thread(self.send_gotify, channel, title, message)
                success_count += 1
            except requests.RequestException as exc:
                logger.error(f"发送 Gotify 通知失败 [{channel['name']}]: {exc}")
            except Exception as exc:
                logger.error(f"发送 Gotify 通知异常 [{channel['name']}]: {exc}")
        return success_count

    async def check_events(self) -> List[RepositoryEvent]:
        async with self.check_lock:
            changes: List[RepositoryEvent] = []
            self.last_notification_counts = {}
            self.last_change_records = []
            for target in self.repositories:
                if not any(
                    (
                        target.monitor_commit,
                        target.monitor_release,
                        target.monitor_prerelease,
                    )
                ):
                    continue
                provider = self.providers[target.platform]
                event_requests = []
                if target.monitor_commit:
                    event_requests.append(
                        ("commit", lambda: provider.fetch_latest_commit(target))
                    )
                if target.monitor_release:
                    event_requests.append(
                        (
                            "release",
                            lambda: provider.fetch_latest_release(target, False),
                        )
                    )
                if target.monitor_prerelease:
                    event_requests.append(
                        (
                            "prerelease",
                            lambda: provider.fetch_latest_release(target, True),
                        )
                    )
                for event_type, fetch in event_requests:
                    try:
                        event = await asyncio.to_thread(fetch)
                        if not event or not event.key:
                            logger.info(
                                f"{target.platform}:{target.repository} 当前没有可用 {event_type}"
                            )
                            continue
                        old_key = (
                            self.state.get(target.target_key, {})
                            .get(event_type, {})
                            .get("key")
                        )
                        first_run = old_key is None
                        sent = 0
                        if old_key != event.key and (
                            not first_run or self.notify_on_first_run
                        ):
                            sent = await self.notify_event(target, event)
                            changes.append(event)
                            self.last_change_records.append((target, event, sent))
                            self.last_notification_counts[
                                f"{target.target_key}:{event.event_type}"
                            ] = sent
                        elif first_run:
                            logger.info(
                                f"首次记录 {target.target_key} 的 {event_type}: {event.key}"
                            )
                        state_item = {
                            "key": event.key,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                        if event.event_type == "commit":
                            state_item["title"] = event.title
                        else:
                            state_item["tag_name"] = event.version
                        self.state.setdefault(target.target_key, {})[event_type] = (
                            state_item
                        )
                    except requests.RequestException as exc:
                        logger.error(
                            f"检查 {target.platform} {event_type} 失败 [{target.repository}]: {exc}"
                        )
                    except Exception as exc:
                        logger.error(
                            f"检查 {target.platform} {event_type} 异常 [{target.repository}]: {exc}"
                        )

            self.last_check_at = datetime.now(timezone.utc).isoformat()
            try:
                await self.save_state()
            except OSError as exc:
                logger.error(f"保存 Release 状态失败: {exc}")
            return changes

    async def check_releases(self) -> List[str]:
        await self.check_events()
        return [
            f"{target.platform}:{target.repository} {event.event_type} "
            f"{event.version or event.key[:12]} (已发送 "
            f"{sent} 个渠道)"
            for target, event, sent in self.last_change_records
        ]

    async def monitor_loop(self) -> None:
        try:
            while True:
                try:
                    await self.check_releases()
                except Exception as exc:
                    logger.error(f"Release 定时监控检查异常: {exc}")
                await asyncio.sleep(self.interval_minutes * 60)
        except asyncio.CancelledError:
            raise

    async def initialize(self):
        await self.load_state()
        if not self.repositories or not any(
            target.monitor_commit or target.monitor_release or target.monitor_prerelease
            for target in self.repositories
        ):
            logger.warning("Release Monitor 未配置有效的监控事件")
            return
        if not self.gotify_channels:
            logger.warning("Release Monitor 未配置有效的 Gotify 渠道")
        self.monitor_task = asyncio.create_task(self.monitor_loop())
        logger.info(
            f"Release Monitor 已启动, 监控 {len(self.repositories)} 个仓库, "
            f"检查间隔 {self.interval_minutes} 分钟"
        )

    @filter.command("release_check")
    async def release_check(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return
        changes = await self.check_releases()
        if changes:
            yield event.plain_result("发现新事件:\n" + "\n".join(changes))
        else:
            yield event.plain_result("检查完成, 暂无新事件")

    @filter.command("release_list")
    async def release_list(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return
        if not self.repositories:
            yield event.plain_result("当前没有配置监控仓库")
            return
        lines = [f"当前监控仓库 ({len(self.repositories)} 个): "]
        for target in self.repositories:
            enabled = []
            if target.monitor_commit:
                enabled.append("Commit")
            if target.monitor_release:
                enabled.append("Release")
            if target.monitor_prerelease:
                enabled.append("Pre-release")
            mode = ", ".join(enabled) if enabled else "未启用事件"
            lines.append(
                f"- {target.platform}:{target.repository} "
                f"[{target.branch or '默认分支'}] [{mode}]"
            )
            for event_type in ("commit", "release", "prerelease"):
                display_name = EVENT_DISPLAY_NAMES[event_type]
                if display_name not in enabled:
                    continue
                item = self.state.get(target.target_key, {}).get(event_type, {})
                if event_type == "commit":
                    value = (
                        item.get("version") or item.get("key", "")[:12] or "尚未检查"
                    )
                else:
                    value = item.get("tag_name") or item.get("key") or "尚未检查"
                lines.append(f"  {event_type}: {value}")
        yield event.plain_result("\n".join(lines))

    @filter.command("release_status")
    async def release_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return
        checked = self.last_check_at or "尚未检查"
        running = self.monitor_task and not self.monitor_task.done()
        github_count = sum(target.platform == "github" for target in self.repositories)
        gitlab_count = sum(target.platform == "gitlab" for target in self.repositories)
        commit_count = sum(target.monitor_commit for target in self.repositories)
        release_count = sum(target.monitor_release for target in self.repositories)
        prerelease_count = sum(
            target.monitor_prerelease for target in self.repositories
        )
        yield event.plain_result(
            f"监控状态: {'运行中' if running else '未运行'}\n"
            f"仓库数量: {len(self.repositories)}\n"
            f"GitHub 仓库: {github_count}\n"
            f"GitLab 仓库: {gitlab_count}\n"
            f"Commit 监控: {commit_count}\n"
            f"Release 监控: {release_count}\n"
            f"Pre-release 监控: {prerelease_count}\n"
            f"Gotify 渠道: {len(self.gotify_channels)}\n"
            f"最后检查: {checked}"
        )

    async def terminate(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        self.monitor_task = None
