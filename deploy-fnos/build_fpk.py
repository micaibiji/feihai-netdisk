#!/usr/bin/env python3
"""Build a fnOS FPK package without requiring fnpack on Windows."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import tarfile
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy-fnos"
TEMPLATE = DEPLOY / "package"
BUILD = DEPLOY / ".build"
STAGE = BUILD / "feihai-drive"
APP_STAGE = BUILD / "app"
DIST = DEPLOY / "dist"
VERSION = "1.0.24"
OUTPUT = DIST / f"feihai-drive_{VERSION}_all.fpk"


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


def normalize_permissions(base: Path) -> None:
    for path in base.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif "cmd" in path.relative_to(base).parts:
            path.chmod(0o755)
        else:
            path.chmod(0o644)


def add_tree(archive: tarfile.TarFile, base: Path) -> None:
    for path in sorted(base.rglob("*"), key=lambda item: item.as_posix()):
        archive.add(path, arcname=path.relative_to(base).as_posix(), recursive=False)


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

    docker_dir = STAGE / "app" / "docker"
    shutil.copy2(ROOT / "Dockerfile", docker_dir / "Dockerfile")
    shutil.copy2(ROOT / "requirements.txt", docker_dir / "requirements.txt")
    copy_tree(ROOT / "app", docker_dir / "app")

    for size in (16, 32, 64, 128, 256):
        write_png(STAGE / "app" / "ui" / "images" / f"icon_{size}.png", size)
    write_png(STAGE / "ICON.PNG", 64)
    write_png(STAGE / "ICON_256.PNG", 256)

    APP_STAGE.mkdir(parents=True)
    shutil.move(str(STAGE / "app"), str(APP_STAGE / "app"))
    normalize_permissions(APP_STAGE / "app")
    app_tgz = STAGE / "app.tgz"
    with tarfile.open(app_tgz, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_tree(archive, APP_STAGE / "app")

    checksum = hashlib.md5(app_tgz.read_bytes()).hexdigest()
    manifest = STAGE / "manifest"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("checksum=", f"checksum={checksum}"), encoding="utf-8", newline="\n")

    normalize_permissions(STAGE)
    if OUTPUT.exists():
        OUTPUT.unlink()
    with tarfile.open(OUTPUT, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_tree(archive, STAGE)

    validate()
    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    print(f"已生成：{OUTPUT}")
    print(f"大小：{size_mb:.2f} MB")
    print(f"app.tgz MD5：{checksum}")


if __name__ == "__main__":
    main()
