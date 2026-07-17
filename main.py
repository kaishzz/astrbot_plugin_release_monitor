import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


ReleaseInfo = Dict[str, Any]


@register(
    "astrbot_plugin_release_monitor",
    "kaish",
    "监控多个 GitHub 仓库的新 Release, 并通过 Gotify 通知",
    "1.0",
)
class ReleaseMonitorPlugin(Star):
    STATE_FILENAME = "release_state.json"
    DEFAULT_INTERVAL_MINUTES = 30

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.repositories = self.parse_repositories(config.get("repositories", []))
        self.github_token = self.normalize_text(config.get("github_token"))
        self.interval_minutes = self.read_int(
            "check_interval_minutes", self.DEFAULT_INTERVAL_MINUTES, minimum=1
        )
        self.include_prereleases = self.read_bool("include_prereleases", False)
        self.notify_on_first_run = self.read_bool("notify_on_first_run", False)
        self.gotify_channels = self.parse_gotify_channels(
            config.get("gotify_channels", [])
        )
        self.state: Dict[str, Dict[str, Any]] = {}
        self.state_lock = asyncio.Lock()
        self.check_lock = asyncio.Lock()
        self.monitor_task: Optional[asyncio.Task] = None
        self.last_check_at: Optional[str] = None

    @staticmethod
    def normalize_text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def parse_repositories(cls, value: Any) -> List[str]:
        if isinstance(value, str):
            values = value.replace(",", "\n").splitlines()
        elif isinstance(value, list):
            values = value
        else:
            values = []

        result: List[str] = []
        seen = set()
        for item in values:
            repo = cls.normalize_text(item).strip("/")
            if repo.startswith("https://github.com/"):
                repo = repo.removeprefix("https://github.com/").split("/releases", 1)[0]
            if repo.count("/") != 1 or repo in seen:
                continue
            owner, name = repo.split("/", 1)
            if owner and name and all(part not in {".", ".."} for part in (owner, name)):
                result.append(repo)
                seen.add(repo)
        return result

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
            url = cls.normalize_text(raw.get("url")).rstrip("/")
            token = cls.normalize_text(raw.get("token"))
            if not url or not token or not url.startswith(("http://", "https://")):
                logger.warning(f"Gotify 渠道 {index} 配置不完整, 已跳过")
                continue
            try:
                priority = int(raw.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            channels.append(
                {
                    "name": cls.normalize_text(raw.get("name")) or f"Gotify {index}",
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
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_state_path(self) -> Path:
        plugin_name = getattr(self, "name", "astrbot_plugin_release_monitor")
        return Path(os.fspath(get_astrbot_data_path())) / "plugin_data" / plugin_name / self.STATE_FILENAME

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
            async with self.state_lock:
                self.state = data
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"读取 Release 持久化文件失败: {path}, {exc}")

    async def save_state(self) -> None:
        async with self.state_lock:
            snapshot = dict(self.state)
        await asyncio.to_thread(self.write_json, self.get_state_path(), snapshot)

    def _github_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def fetch_latest_release(self, repo: str) -> Optional[ReleaseInfo]:
        if self.include_prereleases:
            url = f"https://api.github.com/repos/{quote(repo, safe='/')}/releases?per_page=20"
        else:
            url = f"https://api.github.com/repos/{quote(repo, safe='/')}/releases/latest"
        response = requests.get(url, headers=self._github_headers(), timeout=20)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return next((item for item in payload if isinstance(item, dict) and not item.get("draft")), None)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def release_key(release: ReleaseInfo) -> str:
        return str(release.get("id") or release.get("tag_name") or release.get("html_url"))

    @staticmethod
    def format_release_message(repo: str, release: ReleaseInfo) -> Tuple[str, str]:
        tag = release.get("tag_name") or "未知版本"
        title = release.get("name") or tag
        published = release.get("published_at") or release.get("created_at") or "未知时间"
        url = release.get("html_url") or f"https://github.com/{repo}/releases"
        body = release.get("body") or "（无更新说明）"
        return f"GitHub Release: {repo} {tag}", f"{title}\n发布时间: {published}\n\n{body}\n\n{url}"

    def send_gotify(self, channel: Dict[str, Any], title: str, message: str) -> None:
        endpoint = f"{channel['url']}/message?token={quote(channel['token'], safe='')}"
        response = requests.post(
            endpoint,
            json={"title": title, "message": message, "priority": channel["priority"]},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        response.raise_for_status()

    async def notify(self, repo: str, release: ReleaseInfo) -> int:
        title, message = self.format_release_message(repo, release)
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

    async def check_releases(self) -> List[str]:
        async with self.check_lock:
            changes: List[str] = []
            for repo in self.repositories:
                try:
                    release = await asyncio.to_thread(self.fetch_latest_release, repo)
                    if not release:
                        logger.info(f"{repo} 当前没有可用 Release")
                        continue
                    key = self.release_key(release)
                    old_key = self.state.get(repo, {}).get("release_key")
                    is_new = old_key is not None and old_key != key
                    first_run = old_key is None
                    if is_new or (first_run and self.notify_on_first_run):
                        sent = await self.notify(repo, release)
                        changes.append(f"{repo}: {release.get('tag_name', key)}（已发送 {sent} 个渠道）")
                    elif first_run:
                        logger.info(f"首次记录 {repo} 的 Release: {release.get('tag_name', key)}")

                    self.state[repo] = {
                        "release_key": key,
                        "tag_name": release.get("tag_name"),
                        "published_at": release.get("published_at"),
                        "html_url": release.get("html_url"),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                except requests.RequestException as exc:
                    logger.error(f"检查 GitHub Release 失败 [{repo}]: {exc}")
                except Exception as exc:
                    logger.error(f"检查 GitHub Release 异常 [{repo}]: {exc}")

            self.last_check_at = datetime.now(timezone.utc).isoformat()
            try:
                await self.save_state()
            except OSError as exc:
                logger.error(f"保存 Release 状态失败: {exc}")
            return changes

    async def monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_minutes * 60)
                await self.check_releases()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Release 定时监控任务异常: {exc}")

    async def initialize(self):
        await self.load_state()
        if not self.repositories:
            logger.warning("Release Monitor 未配置有效的 GitHub 仓库")
            return
        if not self.gotify_channels:
            logger.warning("Release Monitor 未配置有效的 Gotify 渠道")
        await self.check_releases()
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
            yield event.plain_result("发现新 Release:\n" + "\n".join(changes))
        else:
            yield event.plain_result("检查完成, 暂无新 Release")

    @filter.command("release_list")
    async def release_list(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return
        if not self.repositories:
            yield event.plain_result("当前没有配置监控仓库")
            return
        lines = [f"当前监控仓库（{len(self.repositories)} 个）："]
        for repo in self.repositories:
            item = self.state.get(repo, {})
            lines.append(f"- {repo}: {item.get('tag_name', '尚未检查')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("release_status")
    async def release_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return
        checked = self.last_check_at or "尚未检查"
        running = self.monitor_task and not self.monitor_task.done()
        yield event.plain_result(
            f"监控状态：{'运行中' if running else '未运行'}\n"
            f"仓库数量：{len(self.repositories)}\n"
            f"Gotify 渠道：{len(self.gotify_channels)}\n"
            f"最后检查：{checked}"
        )

    async def terminate(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        self.monitor_task = None
