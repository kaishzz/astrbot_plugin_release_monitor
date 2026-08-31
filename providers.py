import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import quote

import requests

PRERELEASE_TAG_PATTERN = re.compile(
    r"-(alpha|beta|rc|dev|nightly|preview|pre)(?:\.?\d+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RepositoryTarget:
    platform: str
    repository: str
    branch: str = ""
    monitor_commit: bool = False
    monitor_release: bool = False
    monitor_prerelease: bool = False

    @property
    def target_key(self) -> str:
        return f"{self.platform}:{self.repository}"


@dataclass(frozen=True)
class RepositoryEvent:
    event_type: str
    key: str
    title: str
    version: str
    author: str
    published_at: str
    url: str
    branch: str = ""


class RepositoryProvider(Protocol):
    def fetch_latest_commit(
        self, target: RepositoryTarget
    ) -> Optional[RepositoryEvent]: ...

    def fetch_latest_release(
        self, target: RepositoryTarget, prerelease: bool
    ) -> Optional[RepositoryEvent]: ...


def normalize_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_gitlab_prerelease_tag(tag: str) -> bool:
    return bool(PRERELEASE_TAG_PATTERN.search(normalize_text(tag)))


def _repository_from_value(value: Any) -> str:
    repository = normalize_text(value).strip("/")
    for prefix in ("https://github.com/", "https://gitlab.com/"):
        if repository.startswith(prefix):
            repository = repository.removeprefix(prefix)
            break
    repository = re.split(r"/(?:-|releases|commit|commits)(?:/|$)", repository, 1)[0]
    return repository.strip("/")


def parse_target(item: Any, legacy_mode: bool = False) -> Optional[RepositoryTarget]:
    if isinstance(item, str):
        item = {"repo": item}
        legacy_mode = True
    if not isinstance(item, dict):
        return None

    raw_repository = item.get("repository", item.get("repo"))
    repository = _repository_from_value(raw_repository)
    platform = normalize_text(item.get("platform")).lower()
    if legacy_mode:
        platform = "github"
    elif not platform:
        platform = (
            "gitlab" if "gitlab.com/" in normalize_text(raw_repository) else "github"
        )
    if platform not in {"github", "gitlab"}:
        return None
    parts = repository.split("/")
    if (platform == "github" and len(parts) != 2) or (
        platform == "gitlab" and len(parts) < 2
    ):
        return None
    if any(not part or part in {".", ".."} for part in parts):
        return None

    explicit_flags = any(
        key in item
        for key in ("monitor_commit", "monitor_release", "monitor_prerelease")
    )
    if legacy_mode or (
        not explicit_flags and ("repo" in item or "include_prereleases" in item)
    ):
        monitor_release = True
        monitor_prerelease = parse_bool(item.get("include_prereleases"), False)
    else:
        monitor_release = parse_bool(item.get("monitor_release"), False)
        monitor_prerelease = parse_bool(item.get("monitor_prerelease"), False)

    return RepositoryTarget(
        platform=platform,
        repository=repository,
        branch=normalize_text(item.get("branch")),
        monitor_commit=parse_bool(item.get("monitor_commit"), False),
        monitor_release=monitor_release,
        monitor_prerelease=monitor_prerelease,
    )


def parse_targets(value: Any) -> List[RepositoryTarget]:
    if not isinstance(value, list):
        return []
    result: List[RepositoryTarget] = []
    seen = set()
    for item in value:
        legacy_mode = isinstance(item, str) or (
            isinstance(item, dict)
            and not any(
                key in item
                for key in ("monitor_commit", "monitor_release", "monitor_prerelease")
            )
            and ("repo" in item or "include_prereleases" in item)
        )
        target = parse_target(item, legacy_mode=legacy_mode)
        if target and target.target_key not in seen:
            result.append(target)
            seen.add(target.target_key)
    return result


def _release_key(release: Dict[str, Any]) -> str:
    return str(
        release.get("id")
        or release.get("tag_name")
        or release.get("html_url")
        or release.get("_links", {}).get("self")
    )


def _author_name(value: Any) -> str:
    if isinstance(value, dict):
        return normalize_text(
            value.get("login") or value.get("name") or value.get("username")
        )
    return normalize_text(value)


class GitHubProvider:
    api_url = "https://api.github.com"

    def __init__(self, token: str = ""):
        self.token = normalize_text(token)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = requests.get(
            f"{self.api_url}{path}", headers=self._headers(), params=params, timeout=20
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def fetch_latest_commit(
        self, target: RepositoryTarget
    ) -> Optional[RepositoryEvent]:
        branch = target.branch
        if not branch:
            repository = self._get(f"/repos/{quote(target.repository, safe='/')}")
            branch = (
                normalize_text(repository.get("default_branch"))
                if isinstance(repository, dict)
                else ""
            )
            if not branch:
                return None
        params: Dict[str, Any] = {"per_page": 1}
        params["sha"] = branch
        payload = self._get(
            f"/repos/{quote(target.repository, safe='/')}/commits", params
        )
        if (
            not isinstance(payload, list)
            or not payload
            or not isinstance(payload[0], dict)
        ):
            return None
        commit = payload[0]
        commit_data = (
            commit.get("commit") if isinstance(commit.get("commit"), dict) else {}
        )
        return RepositoryEvent(
            event_type="commit",
            key=str(commit.get("sha") or ""),
            title="Commit",
            version=str(commit.get("sha") or "")[:12],
            author=_author_name(commit.get("author") or commit_data.get("author")),
            published_at=normalize_text(commit_data.get("committer", {}).get("date"))
            if isinstance(commit_data.get("committer"), dict)
            else "",
            url=normalize_text(commit.get("html_url")),
            branch=branch,
        )

    def fetch_latest_release(
        self, target: RepositoryTarget, prerelease: bool
    ) -> Optional[RepositoryEvent]:
        payload = self._get(
            f"/repos/{quote(target.repository, safe='/')}/releases", {"per_page": 20}
        )
        if not isinstance(payload, list):
            return None
        candidates = [
            item
            for item in payload
            if isinstance(item, dict)
            and not item.get("draft")
            and bool(item.get("prerelease")) == prerelease
        ]
        candidates.sort(
            key=lambda item: normalize_text(
                item.get("published_at") or item.get("created_at")
            ),
            reverse=True,
        )
        release = candidates[0] if candidates else None
        if not release:
            return None
        return RepositoryEvent(
            event_type="prerelease" if prerelease else "release",
            key=_release_key(release),
            title=normalize_text(release.get("name") or release.get("tag_name")),
            version=normalize_text(release.get("tag_name")),
            author=_author_name(release.get("author")),
            published_at=normalize_text(
                release.get("published_at") or release.get("created_at")
            ),
            url=normalize_text(release.get("html_url")),
        )


class GitLabProvider:
    api_url = "https://gitlab.com/api/v4"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = requests.get(f"{self.api_url}{path}", params=params, timeout=20)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _project_path(repository: str) -> str:
        return quote(repository, safe="")

    def fetch_latest_commit(
        self, target: RepositoryTarget
    ) -> Optional[RepositoryEvent]:
        branch = target.branch
        if not branch:
            project = self._get(f"/projects/{self._project_path(target.repository)}")
            branch = (
                normalize_text(project.get("default_branch"))
                if isinstance(project, dict)
                else ""
            )
            if not branch:
                return None
        params: Dict[str, Any] = {"per_page": 1}
        params["ref_name"] = branch
        payload = self._get(
            f"/projects/{self._project_path(target.repository)}/repository/commits",
            params,
        )
        if (
            not isinstance(payload, list)
            or not payload
            or not isinstance(payload[0], dict)
        ):
            return None
        commit = payload[0]
        return RepositoryEvent(
            event_type="commit",
            key=normalize_text(commit.get("id")),
            title="Commit",
            version=normalize_text(commit.get("short_id") or commit.get("id"))[:12],
            author=normalize_text(commit.get("author_name")),
            published_at=normalize_text(
                commit.get("committed_date") or commit.get("created_at")
            ),
            url=normalize_text(commit.get("web_url")),
            branch=branch,
        )

    def fetch_latest_release(
        self, target: RepositoryTarget, prerelease: bool
    ) -> Optional[RepositoryEvent]:
        payload = self._get(
            f"/projects/{self._project_path(target.repository)}/releases",
            {"order_by": "released_at", "sort": "desc", "per_page": 20},
        )
        if not isinstance(payload, list):
            return None
        candidates = [
            item
            for item in payload
            if isinstance(item, dict)
            and not item.get("upcoming_release")
            and is_gitlab_prerelease_tag(normalize_text(item.get("tag_name")))
            == prerelease
        ]
        candidates.sort(
            key=lambda item: normalize_text(
                item.get("released_at") or item.get("created_at")
            ),
            reverse=True,
        )
        release = candidates[0] if candidates else None
        if not release:
            return None
        links = release.get("_links") if isinstance(release.get("_links"), dict) else {}
        return RepositoryEvent(
            event_type="prerelease" if prerelease else "release",
            key=_release_key(release),
            title=normalize_text(release.get("name") or release.get("tag_name")),
            version=normalize_text(release.get("tag_name")),
            author=_author_name(release.get("author")),
            published_at=normalize_text(
                release.get("released_at") or release.get("created_at")
            ),
            url=normalize_text(links.get("self")),
        )
