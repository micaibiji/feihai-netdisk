from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .providers import ProviderRegistry


INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
TMDB_SOURCE_PAGE_SIZE = 20
TMDB_MAX_SOURCE_PAGE = 500
RANKING_PAGE_SIZE = 24
TMDB_GENRES = {
    "action": {"movie": 28, "tv": 10759},
    "animation": {"movie": 16, "tv": 16},
    "comedy": {"movie": 35, "tv": 35},
    "crime": {"movie": 80, "tv": 80},
    "documentary": {"movie": 99, "tv": 99},
    "drama": {"movie": 18, "tv": 18},
    "family": {"movie": 10751, "tv": 10751},
    "mystery": {"movie": 9648, "tv": 9648},
    "romance": {"movie": 10749},
    "scifi": {"movie": 878, "tv": 10765},
}
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


def _ranking_sort_key(item: dict[str, Any]) -> tuple[str, float]:
    date = str(item.get("release_date") or item.get("first_air_date") or "")
    return date, float(item.get("popularity") or 0)


async def _discover_tmdb_media(client: httpx.AsyncClient, media_type: str, *, api_key: str,
                               language: str, region: str, page: int,
                               page_size: int, year: int | None = None,
                               genre: str | None = None, country: str | None = None) -> dict[str, Any]:
    """Return a stable logical page while TMDB itself uses fixed 20-item pages."""
    start = (page - 1) * page_size
    first_source_page = start // TMDB_SOURCE_PAGE_SIZE + 1
    offset = start % TMDB_SOURCE_PAGE_SIZE
    last_source_page = (start + page_size - 1) // TMDB_SOURCE_PAGE_SIZE + 1
    if first_source_page > TMDB_MAX_SOURCE_PAGE:
        return {"items": [], "total_results": 0, "total_pages": 0}

    source_pages = range(first_source_page, min(last_source_page, TMDB_MAX_SOURCE_PAGE) + 1)
    date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
    params = {
        "api_key": api_key,
        "language": language,
        "include_adult": "false",
        "sort_by": f"{date_field}.desc",
        f"{date_field}.lte": datetime.now(UTC).date().isoformat(),
        "vote_count.gte": 10,
    }
    if year:
        params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
        params[f"{date_field}.gte"] = f"{year}-01-01"
        params[f"{date_field}.lte"] = min(
            str(params[f"{date_field}.lte"]), f"{year}-12-31"
        )
    if genre:
        genre_id = TMDB_GENRES.get(genre, {}).get(media_type)
        if not genre_id:
            return {"items": [], "total_results": 0, "total_pages": 0}
        params["with_genres"] = genre_id
    if country:
        params["with_origin_country"] = country
    if media_type == "movie":
        params.update({"include_video": "false"})

    async def fetch(source_page: int) -> dict[str, Any]:
        response = await client.get(
            f"https://api.themoviedb.org/3/discover/{media_type}",
            params={**params, "page": source_page},
        )
        response.raise_for_status()
        return response.json()

    bodies = await asyncio.gather(*(fetch(source_page) for source_page in source_pages))
    total_results = int((bodies[0] if bodies else {}).get("total_results") or 0)
    available_results = min(total_results, TMDB_SOURCE_PAGE_SIZE * TMDB_MAX_SOURCE_PAGE)
    raw_items = [item for body in bodies for item in (body.get("results") or [])]
    raw_items = raw_items[offset:offset + page_size]
    for item in raw_items:
        item["media_type"] = media_type
    raw_items.sort(key=_ranking_sort_key, reverse=True)
    return {
        "items": raw_items,
        "total_results": available_results,
        "total_pages": min(500, math.ceil(available_results / page_size)) if available_results else 0,
    }


async def trending_tmdb(settings: Settings, media_type: str = "all", *, api_key: str | None = None,
                        language: str = "zh-CN", region: str = "CN",
                        page: int = 1, year: int | None = None,
                        genre: str | None = None, country: str | None = None) -> dict[str, Any]:
    effective_key = settings.tmdb_api_key if api_key is None else api_key
    page = max(1, int(page))
    if not effective_key:
        return {"live": False, "items": [], "updated_at": None,
                "message": "请在设置中配置 TMDB API 密钥", "page": page,
                "page_size": RANKING_PAGE_SIZE, "total_pages": 0, "total_results": 0}
    endpoint_type = media_type if media_type in {"movie", "tv"} else "all"
    async with httpx.AsyncClient(timeout=15) as client:
        if endpoint_type == "all":
            movie_page, tv_page = await asyncio.gather(
                _discover_tmdb_media(client, "movie", api_key=effective_key,
                                     language=language, region=region, page=page, page_size=12,
                                     year=year, genre=genre, country=country),
                _discover_tmdb_media(client, "tv", api_key=effective_key,
                                     language=language, region=region, page=page, page_size=12,
                                     year=year, genre=genre, country=country),
            )
            raw_items = movie_page["items"] + tv_page["items"]
            raw_items.sort(key=_ranking_sort_key, reverse=True)
            total_results = movie_page["total_results"] + tv_page["total_results"]
            total_pages = max(movie_page["total_pages"], tv_page["total_pages"])
        else:
            result = await _discover_tmdb_media(
                client, endpoint_type, api_key=effective_key, language=language,
                region=region, page=page, page_size=RANKING_PAGE_SIZE,
                year=year, genre=genre, country=country,
            )
            raw_items = result["items"]
            total_results = result["total_results"]
            total_pages = result["total_pages"]
    rank_start = (page - 1) * RANKING_PAGE_SIZE
    return {
        "live": True,
        "items": [_tmdb_item(item, rank_start + index + 1) for index, item in enumerate(raw_items)],
        "updated_at": datetime.now(UTC).isoformat(),
        "message": "按上映日期与热度排序",
        "page": page,
        "page_size": RANKING_PAGE_SIZE,
        "total_pages": total_pages,
        "total_results": total_results,
        "sort": "date_desc,popularity_desc",
        "filters": {"year": year, "genre": genre or "", "country": country or ""},
    }


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

    def walk(node: Any, inherited_title: str, inherited_source: str) -> None:
        if isinstance(node, dict):
            pieces = [str(node.get(key, "")).strip() for key in ("title", "name", "content", "message")]
            pieces = [piece for piece in pieces if piece and piece.lower() not in {"success", "ok", "true", "result"}]
            title = " ".join(dict.fromkeys(pieces)) or inherited_title
            source = str(node.get("source") or node.get("channel") or node.get("plugin") or inherited_source)
            for child in node.values():
                walk(child, title, source)
            return
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_title, inherited_source)
            return
        if not isinstance(node, str):
            return
        display_title = URL_PATTERN.sub("", inherited_title).strip(" -|，,。") or query
        for url in URL_PATTERN.findall(node):
            output.append((url, display_title, inherited_source))

    walk(value, query, "telegram/netdisk")
    return output


def normalize_search_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def pansou_headers(token: str = "") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def decode_pansou_json(content: bytes) -> dict[str, Any]:
    """Keep one malformed plugin title from breaking the complete PanSou result."""
    body = json.loads(content.decode("utf-8", errors="replace"))
    if not isinstance(body, dict):
        raise ValueError("Pansou 返回了无法识别的结果格式")
    return body


async def login_pansou(base_url: str, username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/api/auth/login",
            json={"username": username, "password": password},
        )
        response.raise_for_status()
        body = decode_pansou_json(response.content)
    token = str(body.get("token") or body.get("data", {}).get("token") or "")
    if not token:
        raise ValueError("Pansou 登录成功但没有返回 Token")
    return token


async def test_pansou_connection(base_url: str, token: str = "") -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    headers = pansou_headers(token)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
        for path in ("/api/health", "/health", "/"):
            response = await client.get(f"{base_url}{path}")
            if response.status_code in {401, 403}:
                response.raise_for_status()
            if response.status_code < 500 and response.status_code != 404:
                return {"connected": True, "endpoint": path, "status_code": response.status_code}
    raise ValueError("无法连接 Pansou，请检查地址、网络和鉴权信息")


async def search_resources(settings: Settings, query: str, *, base_url: str | None = None,
                           api_path: str = "/api/search", source: str = "all",
                           token: str = "") -> list[dict[str, Any]]:
    effective_base_url = (settings.pansou_base_url if base_url is None else base_url).rstrip("/")
    if not effective_base_url:
        raise ValueError("尚未在设置中连接自己的 Pansou")
    payload = {"kw": query, "src": source, "res": "all", "cloud_types": ["115", "baidu", "quark", "mobile"]}
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f"{effective_base_url}{api_path}", json=payload, headers=pansou_headers(token),
        )
        response.raise_for_status()
        body = decode_pansou_json(response.content)
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
        if generic_title:
            title = query.strip()
        normalized_query = normalize_search_title(query)
        normalized_candidate = normalize_search_title(title)
        recognized = bool(
            normalized_query and normalized_candidate
            and (normalized_query in normalized_candidate or normalized_candidate in normalized_query)
        )
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
