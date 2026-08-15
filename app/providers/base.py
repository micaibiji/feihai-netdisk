from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any


class CloudError(RuntimeError):
    pass


class AuthenticationError(CloudError):
    pass


class CapabilityError(CloudError):
    pass


@dataclass(slots=True)
class BrowserSupport:
    playable: bool
    mode: str = "none"
    reason: str = "格式信息不足"


@dataclass(slots=True)
class FolderEntry:
    id: str
    name: str
    path: str


@dataclass(slots=True)
class ShareFile:
    id: str
    name: str
    size: int = 0
    is_dir: bool = False
    parent_id: str = ""
    token: str = ""
    mime_type: str = ""
    pick_code: str = ""
    path: str = ""
    browser: BrowserSupport = field(default_factory=lambda: BrowserSupport(False))

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("token", None)
        value.pop("pick_code", None)
        return value


@dataclass(slots=True)
class ShareInspection:
    provider: str
    share_id: str
    title: str
    extraction_code: str
    files: list[ShareFile]
    secret: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "share_id": self.share_id,
            "title": self.title,
            "files": [item.public() for item in self.files],
            "playable_count": sum(1 for item in self.files if item.browser.playable),
            "episode_progress": episode_progress(self.files),
        }


@dataclass(slots=True)
class SaveResult:
    task_id: str = ""
    saved_ids: list[str] = field(default_factory=list)
    saved_files: list[ShareFile] = field(default_factory=list)
    duplicate: bool = False
    message: str = "保存成功"


@dataclass(slots=True)
class DirectLink:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = "application/octet-stream"
    redirect: bool = False


def credential_payload(raw: str) -> dict[str, str]:
    value = raw.strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise AuthenticationError("凭证 JSON 格式不正确") from error
        if not isinstance(parsed, dict):
            raise AuthenticationError("凭证格式不正确")
        return {str(key): str(item) for key, item in parsed.items() if item is not None}
    return {"credential": value}


def join_path(parent: str, name: str) -> str:
    base = parent if parent.startswith("/") else "/" + parent
    return str(PurePosixPath(base) / name)


_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov"}
_ALL_VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".ts", ".m2ts",
    ".mts", ".flv", ".wmv", ".rm", ".rmvb", ".mpg", ".mpeg", ".vob",
}
_BLOCKED_VIDEO_MARKERS = (
    "hevc", "h265", "h.265", "dolby vision", "dolby.vision", "杜比视界", "dovi", "dv.",
    "truehd", "dts-hd", "dts:x", "av1", "10bit", "10-bit",
)

_SEASON_EPISODE_PATTERNS = (
    re.compile(r"(?<![0-9A-Za-z])S\s*0*(?P<season>\d{1,2})[\s._-]*E\s*0*(?P<episode>\d{1,4})(?!\d)", re.I),
    re.compile(r"第\s*(?P<season>\d{1,2})\s*季.*?第\s*(?P<episode>\d{1,4})\s*[集话]"),
)
_EPISODE_ONLY_PATTERNS = (
    re.compile(r"第\s*0*(?P<episode>\d{1,4})\s*[集话]"),
    re.compile(r"(?:^|[\s._/\-\[(])EP?\s*0*(?P<episode>\d{1,4})(?=$|[\s._/\-\])])", re.I),
)
_SEASON_PATH_PATTERNS = (
    re.compile(r"(?:^|/)(?:S|Season[\s._-]*)0*(?P<season>\d{1,2})(?:/|$)", re.I),
    re.compile(r"(?:^|/)第\s*(?P<season>\d{1,2})\s*季(?:/|$)"),
)


def _explicit_episode(value: str) -> tuple[int, int] | None:
    for pattern in _SEASON_EPISODE_PATTERNS:
        if match := pattern.search(value):
            return int(match.group("season")), int(match.group("episode"))
    season = 1
    for pattern in _SEASON_PATH_PATTERNS:
        if match := pattern.search(value):
            season = int(match.group("season"))
            break
    for pattern in _EPISODE_ONLY_PATTERNS:
        if match := pattern.search(value):
            return season, int(match.group("episode"))
    return None


def _loose_episode(name: str) -> int | None:
    """Read common 01.mp4 / 26_4K.mkv names without treating years as episodes."""
    stem = PurePosixPath(name).stem
    values = [int(value) for value in re.findall(r"(?:^|[\s._\-\[(])0*(\d{1,3})(?=$|[\s._\-\])])", stem)]
    values = [value for value in values if 0 < value < 1000 and value not in {264, 265, 720}]
    return values[-1] if values else None


def episode_progress(files: list[ShareFile]) -> dict[str, Any]:
    """Summarize progress from the real share file listing, never from PanSou text."""
    videos = [
        item for item in files
        if not item.is_dir and PurePosixPath(item.name.lower()).suffix in _ALL_VIDEO_EXTENSIONS
    ]
    explicit = {
        key for item in videos
        if (key := _explicit_episode(item.path or item.name)) is not None
    }
    episodes = set(explicit)
    # Numeric-only names are common in cloud shares. Require at least two distinct
    # values before accepting this looser form, which avoids calling movie sequels
    # such as “流浪地球2.mp4” episode 2.
    loose_values = {value for item in videos if (value := _loose_episode(item.name)) is not None}
    if len(loose_values) >= 2:
        known_seasons = {season for season, _ in explicit}
        default_season = next(iter(known_seasons)) if len(known_seasons) == 1 else 1
        episodes.update((default_season, value) for value in loose_values)
    latest_season = latest_episode = 0
    if episodes:
        latest_season, latest_episode = max(episodes)
    if latest_episode:
        label = (
            f"实际更新至第{latest_season}季第{latest_episode}集"
            if latest_season > 1
            else f"实际更新至第{latest_episode}集"
        )
    elif videos:
        label = f"实际读取到{len(videos)}个视频文件"
    else:
        label = "未读取到视频文件"
    return {
        "verified": True,
        "video_file_count": len(videos),
        "numbered_episode_count": len(episodes),
        "latest_season": latest_season,
        "latest_episode": latest_episode,
        "label": label,
    }


def browser_support(name: str, mime_type: str = "") -> BrowserSupport:
    lower = name.lower()
    suffix = PurePosixPath(lower).suffix
    if any(marker in lower for marker in _BLOCKED_VIDEO_MARKERS):
        return BrowserSupport(False, "none", "视频或音频编码不适合网页直接播放")
    if suffix == ".webm":
        return BrowserSupport(True, "direct", "WebM 可直接由现代浏览器播放")
    if suffix in {".mp4", ".m4v"}:
        return BrowserSupport(True, "direct", "MP4 容器适合网页播放；播放前会再次确认")
    if suffix == ".mov" and ("video/mp4" in mime_type or "h264" in lower or "avc" in lower):
        return BrowserSupport(True, "direct", "H.264 MOV 可尝试网页播放")
    if mime_type.startswith("video/mp4"):
        return BrowserSupport(True, "direct", "服务端标记为 MP4 视频")
    return BrowserSupport(False, "none", "仅 MP4/H.264/AAC 或 WebM 显示播放按钮")


def extraction_code_from_url(url: str, supplied: str = "") -> str:
    if supplied:
        return supplied.strip()
    match = re.search(r"(?:pwd|password|passcode)=([A-Za-z0-9]+)", url, re.I)
    return match.group(1) if match else ""


class CloudAdapter(ABC):
    name: str
    label: str
    root_id: str

    def __init__(self, credential: str):
        self.credential = credential

    @abstractmethod
    async def probe(self) -> dict[str, Any]: ...

    @abstractmethod
    async def list_directories(self, parent_id: str, parent_path: str) -> list[FolderEntry]: ...

    @abstractmethod
    async def create_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry: ...

    @abstractmethod
    async def inspect_share(self, share_url: str, extraction_code: str = "") -> ShareInspection: ...

    @abstractmethod
    async def save_share(
        self, inspection: ShareInspection, target_id: str, target_path: str,
        selected_file_ids: list[str], duplicate_policy: str,
    ) -> SaveResult: ...

    @abstractmethod
    async def direct_link(self, file: ShareFile) -> DirectLink: ...

    @abstractmethod
    async def delete(self, file_ids: list[str], file_paths: list[str] | None = None) -> None: ...

    async def ensure_folder(self, parent_id: str, parent_path: str, name: str) -> FolderEntry:
        for item in await self.list_directories(parent_id, parent_path):
            if item.name == name:
                return item
        return await self.create_folder(parent_id, parent_path, name)

    async def locate_saved_files(
        self, target_id: str, target_path: str, expected_names: list[str]
    ) -> list[ShareFile]:
        return []
