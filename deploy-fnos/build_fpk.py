#!/usr/bin/env python3
"""Build a fnOS FPK package without requiring fnpack on Windows."""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import shutil
import struct
import tarfile
import tomllib
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy-fnos"
TEMPLATE = DEPLOY / "package"
BUILD = DEPLOY / ".build"
STAGE = BUILD / "feihai-drive"
APP_STAGE = BUILD / "app"
DIST = DEPLOY / "dist"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = str(PROJECT["project"]["version"])
OUTPUT = DIST / f"feihai-drive_{VERSION}_all.fpk"
REPOSITORY = "https://github.com/micaibiji/feihai-netdisk"
RAW_REPOSITORY = "https://raw.githubusercontent.com/micaibiji/feihai-netdisk/main"
DOWNLOAD_URL = f"{REPOSITORY}/releases/download/v{VERSION}/{OUTPUT.name}"
TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"
}
TEXT_FILENAMES = {
    "Dockerfile", "config", "install", "main", "manifest", "privilege", "resource", "uninstall", "upgrade"
}


def copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache", ".git", ".env", "data"
        ),
    )


def write_png(path: Path, size: int) -> None:
    """Draw the existing CSS logo using only the Python standard library."""
    pixels = bytearray()
    radius = max(3, size // 5)
    margin = max(1, size // 14)
    bars = (
        (size * 27 // 100, size * 49 // 100, size * 11 // 100, size * 28 // 100),
        (size * 45 // 100, size * 34 // 100, size * 11 // 100, size * 43 // 100),
        (size * 63 // 100, size * 22 // 100, size * 11 // 100, size * 55 // 100),
    )

    def inside_round_rect(x: int, y: int) -> bool:
        if margin + radius <= x < size - margin - radius:
            return margin <= y < size - margin
        if margin + radius <= y < size - margin - radius:
            return margin <= x < size - margin
        cx = margin + radius if x < size // 2 else size - margin - radius - 1
        cy = margin + radius if y < size // 2 else size - margin - radius - 1
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2

    for y in range(size):
        pixels.append(0)
        for x in range(size):
            if not inside_round_rect(x, y):
                pixels.extend((0, 0, 0, 0))
                continue
            ratio = (x + y) / max(1, 2 * size - 2)
            red = int(16 - 8 * ratio)
            green = int(185 - 41 * ratio)
            blue = int(105 - 37 * ratio)
            color = (red, green, blue, 255)
            for bx, by, bw, bh in bars:
                br = max(1, bw // 2)
                if bx <= x < bx + bw and by <= y < by + bh:
                    if by + br <= y < by + bh - br:
                        color = (255, 255, 255, 255)
                    else:
                        bcy = by + br if y < by + br else by + bh - br - 1
                        bcx = bx + bw // 2
                        if (x - bcx) ** 2 + (y - bcy) ** 2 <= br**2:
                            color = (255, 255, 255, 255)
            pixels.extend(color)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = b"\x89PNG\r\n\x1a\n"
    raw += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(bytes(pixels), 9))
    raw += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def normalize_text_files(base: Path) -> None:
    """Keep packaged text files byte-identical on Windows and Linux checkouts."""
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix and path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_FILENAMES:
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if normalized != raw:
            path.write_bytes(normalized)


def normalize_permissions(base: Path) -> None:
    for path in base.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif "cmd" in path.relative_to(base).parts:
            path.chmod(0o755)
        else:
            path.chmod(0o644)


def normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Remove host-specific metadata so local and CI packages have one checksum."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if "cmd" in Path(info.name).parts else 0o644
    return info


@contextmanager
def reproducible_tar_gz(path: Path) -> Iterator[tarfile.TarFile]:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                yield archive


def add_tree(archive: tarfile.TarFile, base: Path) -> None:
    for path in sorted(base.rglob("*"), key=lambda item: item.as_posix()):
        archive.add(
            path,
            arcname=path.relative_to(base).as_posix(),
            recursive=False,
            filter=normalize_tar_info,
        )


def validate() -> None:
    required = {
        "manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG",
        "cmd/main", "config/privilege", "config/resource", "wizard/install",
    }
    with tarfile.open(OUTPUT, "r:gz") as outer:
        names = set(outer.getnames())
        missing = required - names
        if missing:
            raise RuntimeError(f"FPK 缺少文件：{sorted(missing)}")
        manifest = outer.extractfile("manifest")
        app_file = outer.extractfile("app.tgz")
        if manifest is None or app_file is None:
            raise RuntimeError("无法读取 FPK 核心文件")
        manifest_text = manifest.read().decode("utf-8")
        app_bytes = app_file.read()
        digest = hashlib.md5(app_bytes).hexdigest()
        if f"checksum={digest}" not in manifest_text:
            raise RuntimeError("app.tgz 校验值不一致")


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    DIST.mkdir(parents=True, exist_ok=True)
    copy_tree(TEMPLATE, STAGE)

    manifest_template = STAGE / "manifest"
    manifest_text = manifest_template.read_text(encoding="utf-8")
    if f"version={VERSION}" not in manifest_text:
        raise RuntimeError(
            "版本号不一致：请让 pyproject.toml 与 deploy-fnos/package/manifest 保持相同"
        )

    docker_dir = STAGE / "app" / "docker"
    shutil.copy2(ROOT / "Dockerfile", docker_dir / "Dockerfile")
    shutil.copy2(ROOT / "requirements.txt", docker_dir / "requirements.txt")
    copy_tree(ROOT / "app", docker_dir / "app")

    for size in (16, 32, 64, 128, 256):
        write_png(STAGE / "app" / "ui" / "images" / f"icon_{size}.png", size)
    write_png(STAGE / "ICON.PNG", 64)
    write_png(STAGE / "ICON_256.PNG", 256)
    normalize_text_files(STAGE)

    APP_STAGE.mkdir(parents=True)
    shutil.move(str(STAGE / "app"), str(APP_STAGE / "app"))
    normalize_permissions(APP_STAGE / "app")
    app_tgz = STAGE / "app.tgz"
    with reproducible_tar_gz(app_tgz) as archive:
        add_tree(archive, APP_STAGE / "app")

    checksum = hashlib.md5(app_tgz.read_bytes()).hexdigest()
    manifest = STAGE / "manifest"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("checksum=", f"checksum={checksum}"), encoding="utf-8", newline="\n")

    normalize_permissions(STAGE)
    if OUTPUT.exists():
        OUTPUT.unlink()
    with reproducible_tar_gz(OUTPUT) as archive:
        add_tree(archive, STAGE)

    validate()
    sha256 = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    sha_file = OUTPUT.with_suffix(OUTPUT.suffix + ".sha256")
    sha_file.write_text(f"{sha256}  {OUTPUT.name}\n", encoding="utf-8", newline="\n")

    latest = {
        "app_id": "micaibiji-feihai-drive",
        "appname": "feihai-drive",
        "name": "飞海网盘",
        "version": VERSION,
        "download_url": DOWNLOAD_URL,
        "sha256": sha256,
        "release_url": f"{REPOSITORY}/releases/tag/v{VERSION}",
    }
    (DIST / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    source = [
        {
            "id": "micaibiji-feihai-drive",
            "name": "飞海网盘",
            "version": VERSION,
            "desc": "飞牛 fnOS 私人网盘影视搜索、同盘保存与网页播放平台",
            "author": "飞海",
            "tags": "影音娱乐,网盘工具",
            "icon": f"{RAW_REPOSITORY}/deploy-fnos/icon.svg",
            "download_url": DOWNLOAD_URL,
            "screenshots": [],
        }
    ]
    (ROOT / "micaibiji.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    fnpack = {
        "schema_version": "2.0",
        "source_info": {
            "name": "飞海网盘应用源",
            "author": "飞海",
            "homepage": REPOSITORY,
        },
        "apps": {
            "feihai-drive": {
                "display_name": "飞海网盘",
                "desc": "飞牛 fnOS 私人网盘影视搜索、同盘保存与网页播放平台。",
                "platform": ["all"],
                "categories": ["影音娱乐"],
                "icon_url": f"{RAW_REPOSITORY}/deploy-fnos/icon.svg",
                "run_as": "package",
                "install_type": "",
                "is_docker": True,
                "service_port": "12366",
                "releases": {
                    VERSION: {
                        "changelog": "请查看 GitHub Releases 获取本版本更新说明。",
                        "packages": {
                            "all": {
                                "download_url": DOWNLOAD_URL,
                                "sha256": sha256,
                                "size": OUTPUT.stat().st_size,
                            }
                        },
                    }
                },
            }
        },
    }
    (ROOT / "fnpack.json").write_text(
        json.dumps(fnpack, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"已生成：{OUTPUT}")
    print(f"大小：{size_mb:.2f} MB")
    print(f"app.tgz MD5：{checksum}")
    print(f"FPK SHA256：{sha256}")


if __name__ == "__main__":
    main()
