from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .providers import ProviderRegistry


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
EPISODE_PATTERNS = (
    re.compile(r"S(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})", re.I),
    re.compile(r"第\s*(?P<episode>\d{1,4})\s*[集话期]"),
    re.compile(r"更新(?:至|到)?\s*(?P<episode>\d{1,4})", re.I),
    re.compile(r"全\s*(?P<episode>\d{1,4})\s*集"),
)


def safe_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", value).strip(" .")
    return cleaned[:180] or "未命名"


def media_folder(title: str, media_type: str = "movie", year: str | int | None = None) -> str:
    category = {"movie": "电影", "tv": "电视剧", "anime": "动漫", "documentary": "纪录片", "variety": "综艺"}.get(media_type, "未分类")
    suffix = f" ({year})" if year else ""
    return f"{category}/{safe_name(title)}{suffix}"


def media_relative_path(title: str, media_type: str, year: int | None = None, season: int = 1, episode: int = 0) -> str:
    base = media_folder(title, media_type, year)
    display = f"{safe_name(title)} ({year})" if year else safe_name(title)
    if media_type == "movie":
        return f"{base}/{display}.strm"
    return f"{base}/Season {season:02d}/{display} - S{season:02d}E{episode:02d}.strm"


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


def create_media_bundle(settings: Settings, *, title: str, media_type: str, play_url: str, year: int | None = None, season: int = 1, episode: int = 0, overview: str = "") -> dict[str, str]:
    relative = Path(media_relative_path(title, media_type, year, season, episode))
    target = settings.strm_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(play_url.strip() + "\n", encoding="utf-8")
    nfo = target.with_suffix(".nfo")
    kind = "movie" if media_type == "movie" else "episodedetails"
    nfo.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<{kind}><title>{_xml(title)}</title><year>{year or ""}</year><plot>{_xml(overview)}</plot></{kind}>\n',
        encoding="utf-8",
    )
    return {"strm": str(relative).replace("\\", "/"), "nfo": str(relative.with_suffix(".nfo")).replace("\\", "/")}


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def parse_episode(title: str) -> tuple[int, int]:
    season = 1
    episode = 0
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(title)
        if match:
            season = int(match.groupdict().get("season") or 1)
            episode = int(match.group("episode"))
            break
    return season, episode


def quality_label(title: str) -> str:
    labels = [value for value in ("8K", "4K", "2160P", "1080P", "HDR", "DV", "杜比视界") if value.lower() in title.lower()]
    return " · ".join(dict.fromkeys(labels)) or "未识别"


def _tmdb_item(item: dict[str, Any], rank: int | None = None) -> dict[str, Any]:
    media_type = item.get("media_type") or ("movie" if item.get("title") else "tv")
    date = item.get("release_date") or item.get("first_air_date") or ""
    return {
        "id": item.get("id"), "title": item.get("title") or item.get("name") or "未知影视",
        "media_type": media_type, "date": date, "year": date[:4], "overview": item.get("overview", ""),
        "score": round(float(item.get("vote_average") or 0), 1), "poster": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else "",
        "backdrop": f"https://image.tmdb.org/t/p/original{item['backdrop_path']}" if item.get("backdrop_path") else "",
        "rank": rank,
    }


async def search_tmdb(settings: Settings, query: str, *, api_key: str | None = None,
                      language: str = "zh-CN", region: str = "CN") -> list[dict[str, Any]]:
    effective_key = settings.tmdb_api_key if api_key is None else api_key
    if not effective_key:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://api.themoviedb.org/3/search/multi", params={"api_key": effective_key, "query": query, "language": language, "region": region, "include_adult": "false"})
        response.raise_for_status()
        results = response.json().get("results", [])[:20]
    return [_tmdb_item(item) for item in results if item.get("media_type") in {"movie", "tv"}]


async def trending_tmdb(settings: Settings, media_type: str = "all", *, api_key: str | None = None,
                        language: str = "zh-CN", region: str = "CN",
                        window: str = "day") -> dict[str, Any]:
    effective_key = settings.tmdb_api_key if api_key is None else api_key
    if not effective_key:
        return {"live": False, "items": [], "updated_at": None, "message": "请在设置中配置 TMDB API 密钥"}
    endpoint_type = media_type if media_type in {"movie", "tv"} else "all"
    endpoint_window = window if window in {"day", "week"} else "day"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://api.themoviedb.org/3/trending/{endpoint_type}/{endpoint_window}", params={"api_key": effective_key, "language": language, "region": region})
        response.raise_for_status()
        items = response.json().get("results", [])
    return {"live": True, "items": [_tmdb_item(item, index + 1) for index, item in enumerate(items[:18]) if item.get("media_type", endpoint_type) in {"movie", "tv"}], "updated_at": datetime.now(UTC).isoformat(), "message": "TMDB 实时榜单"}


async def send_notifications(settings: Settings, message: str) -> list[str]:
    delivered: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        if settings.telegram_bot_token and settings.telegram_chat_id:
            response = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", json={"chat_id": settings.telegram_chat_id, "text": message})
            response.raise_for_status()
            delivered.append("telegram")
        if settings.wecom_webhook_url:
            response = await client.post(settings.wecom_webhook_url, json={"msgtype": "text", "text": {"content": message}})
            response.raise_for_status()
            delivered.append("wecom")
    return delivered


def _find_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return URL_PATTERN.findall(value)
    if isinstance(value, dict):
        return [url for child in value.values() for url in _find_urls(child)]
    if isinstance(value, list):
        return [url for child in value for url in _find_urls(child)]
    return []


def _find_contexts(value: Any, query: str) -> list[tuple[str, str, str]]:
    output: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        blob = " ".join(str(value.get(key, "")) for key in ("title", "name", "content", "message"))
        source = str(value.get("source") or value.get("channel") or "telegram/netdisk")
        for url in _find_urls(value):
            output.append((url, blob.strip() or query, source))
        return output
    for url in _find_urls(value):
        output.append((url, query, "telegram/netdisk"))
    return output


async def search_resources(settings: Settings, query: str) -> list[dict[str, Any]]:
    payload = {"kw": query, "src": "all", "res": "all", "cloud_types": ["115", "baidu", "quark", "mobile"]}
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(f"{settings.pansou_base_url}/api/search", json=payload)
        response.raise_for_status()
        body = response.json()
    candidates = body.get("results", body)
    dedup: dict[str, dict[str, Any]] = {}
    for url, title, source in _find_contexts(candidates, query):
        clean_url = url.rstrip(".,，。)）]")
        try:
            provider = ProviderRegistry.detect(clean_url)
        except ValueError:
            continue
        season, episode = parse_episode(title)
        generic_title = title.strip().lower() in {"", "success", "ok", "true", "result"}
        recognized = not generic_title and query.strip().lower() in title.lower()
        fingerprint = hashlib.sha256(clean_url.encode()).hexdigest()
        dedup[fingerprint] = {
            "provider": provider.name.value, "provider_label": provider.label, "title": title,
            "url": clean_url, "source": source, "fingerprint": fingerprint,
            "season": season, "episode": episode, "quality": quality_label(title),
            "risk_status": "unknown", "datetime": datetime.now(UTC).isoformat(),
            "normalized_title": query.strip() if recognized else "",
            "recognition_state": "recognized" if recognized else "pending",
            "validation_state": "pending",
        }
    order = {name: index for index, name in enumerate(settings.provider_priority)}
    return sorted(dedup.values(), key=lambda item: (-item["season"], -item["episode"], order.get(item["provider"], 999)))[:100]


def provider_auth_start(settings: Settings, provider: str) -> dict[str, Any]:
    if provider == "baidu" and settings.baidu_client_id and settings.baidu_redirect_uri:
        query = urlencode({"response_type": "code", "client_id": settings.baidu_client_id, "redirect_uri": settings.baidu_redirect_uri, "scope": "basic,netdisk", "display": "popup", "qrcode": "1"})
        return {"ready": True, "mode": "oauth", "url": f"https://openapi.baidu.com/oauth/2.0/authorize?{query}", "message": "请在百度官方页面授权"}
    if provider in {"115", "quark", "china_mobile"} and settings.openlist_url:
        return {"ready": True, "mode": "gateway", "url": "", "message": "正在准备页面内登录"}
    hints = {
        "115": "115 页面内登录尚未就绪",
        "baidu": "需要填写百度开放平台 Client ID 与回调地址",
        "quark": "夸克页面内登录尚未就绪",
        "china_mobile": "中国移动云盘页面内登录尚未就绪",
    }
    return {"ready": False, "mode": "setup", "url": "", "message": hints[provider]}
