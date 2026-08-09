from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

import httpx

from .providers.registry import ProviderRegistry


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
EPISODE_PATTERNS = (
    re.compile(r"S(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})", re.I),
    re.compile(r"第\s*(?P<episode>\d{1,4})\s*[集话期]"),
    re.compile(r"更新(?:至|到)?\s*(?P<episode>\d{1,4})", re.I),
    re.compile(r"全\s*(?P<episode>\d{1,4})\s*集"),
)
PROVIDER_CHECKER_NAMES = {
    "115": "pan115",
    "baidu": "baidu",
    "quark": "quark",
    "china_mobile": "cmcc",
}
GENRES = {
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


def parse_episode(title: str) -> tuple[int, int]:
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(title)
        if match:
            return int(match.groupdict().get("season") or 1), int(match.group("episode"))
    return 1, 0


def quality_label(title: str) -> str:
    values = ("8K", "4K", "2160P", "1080P", "HDR", "DV", "杜比视界", "H.264", "HEVC")
    labels = [value for value in values if value.lower() in title.lower()]
    return " · ".join(dict.fromkeys(labels)) or "格式待检查"


def normalize_title(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", value).casefold())


def _find_contexts(value: Any, query: str) -> list[tuple[str, str, str, str]]:
    output: list[tuple[str, str, str, str]] = []

    def walk(node: Any, inherited_title: str, inherited_source: str, inherited_code: str) -> None:
        if isinstance(node, dict):
            pieces = [str(node.get(key, "")).strip() for key in ("title", "name", "content", "message", "remark")]
            pieces = [value for value in pieces if value.lower() not in {"", "success", "ok", "true", "result"}]
            title = " ".join(dict.fromkeys(pieces)) or inherited_title
            source = str(node.get("source") or node.get("channel") or node.get("plugin") or inherited_source)
            code = str(node.get("password") or node.get("pwd") or node.get("code") or inherited_code)
            for child in node.values():
                walk(child, title, source, code)
        elif isinstance(node, list):
            for child in node:
                walk(child, inherited_title, inherited_source, inherited_code)
        elif isinstance(node, str):
            title = URL_PATTERN.sub("", inherited_title).strip(" -|，,。") or query
            for url in URL_PATTERN.findall(node):
                output.append((url.rstrip(".,，。)）]"), title, inherited_source, inherited_code))

    walk(value, query, "PanSou", "")
    return output


async def search_pansou(base_url: str, query: str, token: str = "") -> list[dict[str, Any]]:
    if not base_url:
        raise ValueError("请先在设置中填写自己的 PanSou 地址")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"kw": query, "src": "all", "res": "all", "cloud_types": ["115", "baidu", "quark", "mobile"]}
    body: Any = None
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers=headers) as client:
        attempts = (
            ("POST", "/api/search", {"json": payload}),
            ("GET", "/api/search", {"params": {"kw": query}}),
            ("GET", "/search", {"params": {"q": query}}),
        )
        for method, path, kwargs in attempts:
            try:
                response = await client.request(method, f"{base_url.rstrip('/')}{path}", **kwargs)
                if response.status_code in {404, 405}:
                    continue
                response.raise_for_status()
                body = response.json()
                break
            except (httpx.HTTPError, ValueError) as error:
                errors.append(f"{path}: {type(error).__name__}")
    if body is None:
        raise ValueError("PanSou 搜索接口无法使用（" + "；".join(errors[-2:]) + "）")
    candidates = body.get("results", body.get("data", body)) if isinstance(body, dict) else body
    result: dict[str, dict[str, Any]] = {}
    normalized_query = normalize_title(query)
    for url, title, source, code in _find_contexts(candidates, query):
        try:
            provider = ProviderRegistry.detect(url)
        except Exception:
            continue
        season, episode = parse_episode(title)
        normalized_candidate = normalize_title(title)
        fingerprint = hashlib.sha256(url.encode()).hexdigest()
        result[fingerprint] = {
            "provider": provider,
            "provider_label": ProviderRegistry.labels[provider],
            "title": title or query,
            "url": url,
            "extraction_code": code,
            "source": source or "PanSou",
            "fingerprint": fingerprint,
            "season": season,
            "episode": episode,
            "quality": quality_label(title),
            "recognized": bool(normalized_query and normalized_query in normalized_candidate),
            "validation_state": "unverifiable",
            "validation_reason": "尚未检测",
        }
    # 只调整相关度，不引入网盘优先级；四个网盘保持业务平等。
    return sorted(
        result.values(),
        key=lambda item: (bool(item["recognized"]), int(item["season"]), int(item["episode"])),
        reverse=True,
    )[:100]


def _canonical(url: str) -> str:
    return url.strip().rstrip("/")


def _status_lists(body: Any) -> tuple[set[str], set[str], set[str]]:
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if not isinstance(body, dict):
        return set(), set(), set()
    valid = body.get("valid_links") or body.get("validLinks") or body.get("valid") or []
    invalid = body.get("invalid_links") or body.get("invalidLinks") or body.get("invalid") or []
    pending = body.get("pending_links") or body.get("pendingLinks") or body.get("pending") or []
    # 兼容逐条结果格式；只有服务明确给出 invalid/expired 才判失效。
    for item in body.get("results") or body.get("list") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "")
        state = str(item.get("status") or item.get("state") or "").lower()
        if state in {"valid", "ok", "alive", "success"}:
            valid = [*valid, url]
        elif state in {"invalid", "expired", "dead", "failed"}:
            invalid = [*invalid, url]
        else:
            pending = [*pending, url]
    return ({_canonical(str(x)) for x in valid}, {_canonical(str(x)) for x in invalid}, {_canonical(str(x)) for x in pending})


async def check_links(base_url: str, urls: list[str], token: str = "") -> dict[str, dict[str, str]]:
    unique = list(dict.fromkeys(url for url in urls if url))
    fallback = {url: {"state": "unverifiable", "reason": "检测网站暂不可用，保留显示"} for url in unique}
    if not base_url or not unique:
        return fallback
    platforms = []
    for url in unique:
        try:
            name = PROVIDER_CHECKER_NAMES.get(ProviderRegistry.detect(url))
        except Exception:
            name = None
        if name and name not in platforms:
            platforms.append(name)
    payload = {"links": unique, "selectedPlatforms": platforms}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body: Any = None
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers=headers) as client:
        for path in ("/api/v1/links/check", "/api/check", "/check"):
            try:
                response = await client.post(f"{base_url.rstrip('/')}{path}", json=payload)
                if response.status_code in {404, 405}:
                    continue
                response.raise_for_status()
                body = response.json()
                break
            except (httpx.HTTPError, ValueError):
                continue
    if body is None:
        return fallback
    valid, invalid, pending = _status_lists(body)
    result: dict[str, dict[str, str]] = {}
    for url in unique:
        key = _canonical(url)
        if key in invalid:
            result[url] = {"state": "invalid", "reason": "检测网站明确判定链接失效"}
        elif key in valid:
            result[url] = {"state": "valid", "reason": "检测网站确认链接有效"}
        elif key in pending:
            result[url] = {"state": "unverifiable", "reason": "检测网站正在检测，保留显示"}
        else:
            result[url] = fallback[url]
    return result


def tmdb_item(item: dict[str, Any], rank: int | None = None) -> dict[str, Any]:
    media_type = item.get("media_type") or ("movie" if item.get("title") else "tv")
    date = item.get("release_date") or item.get("first_air_date") or ""
    return {
        "id": item.get("id"),
        "title": item.get("title") or item.get("name") or "未知影视",
        "media_type": media_type,
        "date": date,
        "year": date[:4],
        "overview": item.get("overview") or "暂无简介",
        "score": round(float(item.get("vote_average") or 0), 1),
        "poster": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else "",
        "backdrop": f"https://image.tmdb.org/t/p/original{item['backdrop_path']}" if item.get("backdrop_path") else "",
        "rank": rank,
    }


async def search_tmdb(api_key: str, query: str) -> list[dict[str, Any]]:
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.themoviedb.org/3/search/multi",
            params={"api_key": api_key, "query": query, "language": "zh-CN", "region": "CN", "include_adult": "false"},
        )
        response.raise_for_status()
        items = response.json().get("results") or []
    return [tmdb_item(item) for item in items if item.get("media_type") in {"movie", "tv"}][:10]


async def _discover(
    client: httpx.AsyncClient,
    api_key: str,
    media_type: str,
    page: int,
    page_size: int,
    year: int | None,
    genre: str,
    country: str,
) -> dict[str, Any]:
    start = (page - 1) * page_size
    first = start // 20 + 1
    offset = start % 20
    last = (start + page_size - 1) // 20 + 1
    if first > 500:
        return {"items": [], "total": 0, "pages": 0}
    date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
    params: dict[str, Any] = {
        "api_key": api_key,
        "language": "zh-CN",
        "include_adult": "false",
        "sort_by": f"{date_field}.desc",
        f"{date_field}.lte": datetime.now(UTC).date().isoformat(),
        "vote_count.gte": 5,
    }
    if year:
        params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
    if genre:
        genre_id = GENRES.get(genre, {}).get(media_type)
        if not genre_id:
            return {"items": [], "total": 0, "pages": 0}
        params["with_genres"] = genre_id
    if country:
        params["with_origin_country"] = country

    async def fetch(source_page: int) -> dict[str, Any]:
        response = await client.get(f"https://api.themoviedb.org/3/discover/{media_type}", params={**params, "page": source_page})
        response.raise_for_status()
        return response.json()

    bodies = await asyncio.gather(*(fetch(value) for value in range(first, min(last, 500) + 1)))
    raw = [item for body in bodies for item in (body.get("results") or [])][offset:offset + page_size]
    for item in raw:
        item["media_type"] = media_type
    raw.sort(key=lambda item: (item.get("release_date") or item.get("first_air_date") or "", float(item.get("popularity") or 0)), reverse=True)
    total = min(int((bodies[0] if bodies else {}).get("total_results") or 0), 10000)
    return {"items": raw, "total": total, "pages": min(500, math.ceil(total / page_size)) if total else 0}


async def rankings(
    api_key: str,
    media_type: str = "all",
    page: int = 1,
    year: int | None = None,
    genre: str = "",
    country: str = "",
) -> dict[str, Any]:
    page = max(1, page)
    if not api_key:
        return {"live": False, "message": "管理员还没有配置 TMDB API 密钥", "items": [], "page": page, "total_pages": 0, "total_results": 0}
    async with httpx.AsyncClient(timeout=20) as client:
        if media_type in {"movie", "tv"}:
            result = await _discover(client, api_key, media_type, page, 24, year, genre, country)
            raw, total, pages = result["items"], result["total"], result["pages"]
        else:
            movie, tv = await asyncio.gather(
                _discover(client, api_key, "movie", page, 12, year, genre, country),
                _discover(client, api_key, "tv", page, 12, year, genre, country),
            )
            raw = movie["items"] + tv["items"]
            raw.sort(key=lambda item: (item.get("release_date") or item.get("first_air_date") or "", float(item.get("popularity") or 0)), reverse=True)
            total, pages = movie["total"] + tv["total"], max(movie["pages"], tv["pages"])
    start = (page - 1) * 24
    return {
        "live": True,
        "message": "按上映日期从新到旧；同日按热度排序",
        "items": [tmdb_item(item, start + index + 1) for index, item in enumerate(raw)],
        "page": page,
        "page_size": 24,
        "total_pages": pages,
        "total_results": total,
        "filters": {"type": media_type, "year": year, "genre": genre, "country": country},
    }


async def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    if not bot_token or not chat_id:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
        )
        response.raise_for_status()
    return True
