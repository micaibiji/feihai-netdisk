from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

from .providers.base import BrowserSupport, CapabilityError, CloudError, ShareFile, ShareInspection, browser_support


@dataclass(slots=True)
class MagnetFile:
    index: int
    name: str
    relative_path: str
    size: int


def _text(value: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def bdecode(data: bytes) -> Any:
    """Small, strict bencode decoder used for aria2-produced torrent metadata."""

    def parse(position: int) -> tuple[Any, int]:
        if position >= len(data):
            raise ValueError("磁力元数据不完整")
        marker = data[position:position + 1]
        if marker == b"i":
            end = data.index(b"e", position + 1)
            return int(data[position + 1:end]), end + 1
        if marker == b"l":
            values: list[Any] = []
            position += 1
            while data[position:position + 1] != b"e":
                value, position = parse(position)
                values.append(value)
            return values, position + 1
        if marker == b"d":
            values: dict[bytes, Any] = {}
            position += 1
            while data[position:position + 1] != b"e":
                key, position = parse(position)
                if not isinstance(key, bytes):
                    raise ValueError("磁力元数据键值无效")
                values[key], position = parse(position)
            return values, position + 1
        if marker.isdigit():
            colon = data.index(b":", position)
            length = int(data[position:colon])
            start, end = colon + 1, colon + 1 + length
            if end > len(data):
                raise ValueError("磁力元数据字符串不完整")
            return data[start:end], end
        raise ValueError("无法识别磁力元数据")

    result, position = parse(0)
    if position != len(data):
        raise ValueError("磁力元数据包含多余内容")
    return result


def magnet_info_hash(magnet_url: str) -> str:
    if not magnet_url.lower().startswith("magnet:?"):
        raise ValueError("不是有效的磁力链接")
    values = parse_qs(urlparse(magnet_url).query).get("xt") or []
    value = next((item.split(":")[-1] for item in values if item.lower().startswith("urn:btih:")), "")
    if len(value) == 40:
        try:
            bytes.fromhex(value)
        except ValueError as error:
            raise ValueError("磁力链接的 BTIH 无效") from error
        return value.lower()
    if len(value) == 32:
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError as error:
            raise ValueError("磁力链接的 BTIH 无效") from error
    raise ValueError("磁力链接缺少可用的 BTIH")


def torrent_files(torrent_path: Path) -> tuple[str, list[MagnetFile]]:
    root = bdecode(torrent_path.read_bytes())
    if not isinstance(root, dict) or not isinstance(root.get(b"info"), dict):
        raise ValueError("种子文件缺少 info 信息")
    info: dict[bytes, Any] = root[b"info"]
    raw_title = info.get(b"name.utf-8") or info.get(b"name")
    title = _text(raw_title) if isinstance(raw_title, bytes) else "磁力资源"
    output: list[MagnetFile] = []
    raw_files = info.get(b"files")
    if isinstance(raw_files, list):
        for index, item in enumerate(raw_files, start=1):
            if not isinstance(item, dict):
                continue
            parts = item.get(b"path.utf-8") or item.get(b"path") or []
            names = [_text(part) for part in parts if isinstance(part, bytes)]
            relative = str(PurePosixPath(title, *names))
            output.append(MagnetFile(index, names[-1] if names else f"文件{index}", relative, int(item.get(b"length") or 0)))
    else:
        output.append(MagnetFile(1, title, title, int(info.get(b"length") or 0)))
    return title, output


class MagnetService:
    def __init__(self, data_dir: Path, max_bytes: int):
        self.metadata_dir = data_dir / "magnet-metadata"
        self.cache_dir = data_dir / "magnet-cache"
        self.max_bytes = max_bytes
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def _run(self, *arguments: str, timeout: float | None = None) -> tuple[int, str]:
        if not shutil.which("aria2c"):
            raise CapabilityError("磁力播放组件尚未安装，请重新构建飞海网盘容器")
        process = await asyncio.create_subprocess_exec(
            "aria2c", *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise CapabilityError("获取磁力文件列表超时，请稍后重试或更换资源")
        return process.returncode or 0, output.decode("utf-8", errors="replace")[-2000:]

    async def metadata(self, magnet_url: str) -> Path:
        info_hash = magnet_info_hash(magnet_url)
        torrent_path = self.metadata_dir / f"{info_hash}.torrent"
        if torrent_path.exists():
            return torrent_path
        code, output = await self._run(
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            "--seed-time=0",
            "--enable-rpc=false",
            "--bt-enable-lpd=false",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--download-result=hide",
            f"--dir={self.metadata_dir}",
            magnet_url,
            timeout=75,
        )
        candidates = [item for item in self.metadata_dir.glob("*.torrent") if item.stem.lower() == info_hash]
        if candidates:
            return candidates[0]
        if code:
            raise CloudError(f"磁力元数据获取失败（aria2 {code}）：{output.splitlines()[-1] if output else '没有可用节点'}")
        raise CloudError("磁力元数据暂时不可用，请更换资源或稍后重试")

    async def inspect(self, magnet_url: str) -> ShareInspection:
        torrent_path = await self.metadata(magnet_url)
        title, items = torrent_files(torrent_path)
        files = [
            ShareFile(
                id=str(item.index),
                name=item.name,
                size=item.size,
                path=item.relative_path,
                mime_type="video/webm" if item.name.lower().endswith(".webm") else "video/mp4" if item.name.lower().endswith((".mp4", ".m4v")) else "",
                browser=browser_support(item.name),
            )
            for item in items
        ]
        return ShareInspection("magnet", magnet_info_hash(magnet_url), title, "", files, {"torrent_path": str(torrent_path)})

    async def download(self, magnet_url: str, file_id: str, job_dir: Path) -> tuple[Path, ShareFile]:
        inspection = await self.inspect(magnet_url)
        selected = next((item for item in inspection.files if item.id == file_id), None)
        if not selected or not selected.browser.playable:
            raise CapabilityError("这个磁力资源没有确认适合网页播放的 MP4/H.264/AAC 或 WebM 文件")
        if selected.size > self.max_bytes:
            limit = self.max_bytes / 1024 / 1024 / 1024
            raise CapabilityError(f"所选视频超过磁力临时缓存上限（{limit:.0f} GB）")
        torrent_path = Path(inspection.secret["torrent_path"])
        job_dir.mkdir(parents=True, exist_ok=True)
        code, output = await self._run(
            "--seed-time=0",
            "--enable-rpc=false",
            "--bt-enable-lpd=false",
            "--file-allocation=none",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--download-result=hide",
            f"--select-file={file_id}",
            f"--dir={job_dir}",
            str(torrent_path),
            timeout=None,
        )
        relative = Path(*PurePosixPath(selected.path).parts)
        local_path = job_dir / relative
        if not local_path.is_file():
            matches = [item for item in job_dir.rglob(selected.name) if item.is_file()]
            local_path = matches[0] if matches else local_path
        if code or not local_path.is_file():
            raise CloudError(f"磁力视频下载失败（aria2 {code}）：{output.splitlines()[-1] if output else '资源没有可用节点'}")
        selected.size = local_path.stat().st_size
        return local_path, selected

    def safe_remove(self, local_path: str) -> None:
        root = self.cache_dir.resolve()
        target = Path(local_path).resolve()
        if target != root and root not in target.parents:
            raise ValueError("拒绝清理磁力缓存目录之外的文件")
        job_dir = next((parent for parent in target.parents if parent.parent == root), None)
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir)
        elif target.exists():
            target.unlink()

    async def probe_codecs(self, local_path: Path) -> BrowserSupport:
        if not shutil.which("ffprobe"):
            return browser_support(local_path.name)
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
            "-of", "json", str(local_path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            return BrowserSupport(False, "none", "无法确认视频编码")
        streams = json.loads(stdout or b"{}").get("streams") or []
        video = next((item.get("codec_name") for item in streams if item.get("codec_type") == "video"), "")
        audio = next((item.get("codec_name") for item in streams if item.get("codec_type") == "audio"), "")
        suffix = local_path.suffix.lower()
        if suffix in {".mp4", ".m4v"} and video == "h264" and audio in {"", "aac", "mp3"}:
            return BrowserSupport(True, "local", "已确认 MP4/H.264/AAC 网页兼容格式")
        if suffix == ".webm" and video in {"vp8", "vp9", "av1"} and audio in {"", "opus", "vorbis"}:
            return BrowserSupport(True, "local", "已确认 WebM 网页兼容格式")
        return BrowserSupport(False, "none", f"实际编码 {video or '未知'}/{audio or '无音频'} 不适合网页直接播放")
