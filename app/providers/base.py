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
_BLOCKED_VIDEO_MARKERS = (
    "hevc", "h265", "h.265", "dolby vision", "dolby.vision", "杜比视界", "dovi", "dv.",
    "truehd", "dts-hd", "dts:x", "av1", "10bit", "10-bit",
)


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
