from __future__ import annotations

import re
import json
import hashlib
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .providers import ProviderRegistry


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip(" .")
    return cleaned[:180] or "未命名"


def media_folder(title: str, media_type: str = "movie", year: str | int | None = None) -> str:
    category = {
        "movie": "电影",
        "tv": "电视剧",
        "anime": "动漫",
        "documentary": "纪录片",
        "variety": "综艺",
    }.get(media_type, "未分类")
    suffix = f" ({year})" if year else ""
    return f"{category}/{safe_name(title)}{suffix}"


def generate_strm(settings: Settings, relative_dir: str, name: str, play_url: str) -> Path:
    base = settings.strm_dir.resolve()
    relative_parts = [safe_name(part) for part in Path(relative_dir).parts if part not in (".", "..", "")]
    target_dir = base.joinpath(*relative_parts).resolve()
    if base != target_dir and base not in target_dir.parents:
        raise ValueError("STRM目录不安全")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_name(name)}.strm"
    target.write_text(play_url.strip() + "\n", encoding="utf-8")
    return target


async def search_tmdb(settings: Settings, query: str) -> list[dict[str, Any]]:
    if not settings.tmdb_api_key:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.themoviedb.org/3/search/multi",
            params={"api_key": settings.tmdb_api_key, "query": query, "language": "zh-CN"},
        )
        response.raise_for_status()
        results = response.json().get("results", [])[:10]
    return [
        {
            "id": item.get("id"),
            "title": item.get("title") or item.get("name"),
            "media_type": item.get("media_type"),
            "date": item.get("release_date") or item.get("first_air_date"),
            "overview": item.get("overview", ""),
        }
        for item in results
        if item.get("media_type") in {"movie", "tv"}
    ]


async def send_notifications(settings: Settings, message: str) -> list[str]:
    delivered: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        if settings.telegram_bot_token and settings.telegram_chat_id:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": message},
            )
            response.raise_for_status()
            delivered.append("telegram")
        if settings.wecom_webhook_url:
            response = await client.post(
                settings.wecom_webhook_url,
                json={"msgtype": "text", "text": {"content": message}},
            )
            response.raise_for_status()
            delivered.append("wecom")
    return delivered


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


def _find_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return URL_PATTERN.findall(value)
    if isinstance(value, dict):
        found: list[str] = []
        for child in value.values():
            found.extend(_find_urls(child))
        return found
    if isinstance(value, list):
        found = []
        for child in value:
            found.extend(_find_urls(child))
        return found
    return []


async def search_resources(settings: Settings, query: str) -> list[dict[str, Any]]:
    payload = {
        "kw": query,
        "src": "all",
        "res": "all",
        "cloud_types": ["115", "baidu", "quark", "mobile"],
    }
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(f"{settings.pansou_base_url}/api/search", json=payload)
        response.raise_for_status()
        body = response.json()

    candidates = body.get("results", body)
    urls = list(dict.fromkeys(_find_urls(candidates)))
    results: list[dict[str, Any]] = []
    serialized = json.dumps(candidates, ensure_ascii=False)
    for url in urls:
        try:
            provider = ProviderRegistry.detect(url.rstrip(".,，。)）]"))
        except ValueError:
            continue
        clean_url = url.rstrip(".,，。)）]")
        results.append(
            {
                "provider": provider.name.value,
                "provider_label": provider.label,
                "title": query,
                "url": clean_url,
                "source": "telegram/netdisk",
                "fingerprint": hashlib.sha256(clean_url.encode()).hexdigest(),
            }
        )

    order = {name: index for index, name in enumerate(settings.provider_priority)}
    results.sort(key=lambda item: order.get(item["provider"], 999))
    return results[:100]
