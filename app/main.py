from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urljoin

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .integrations import check_links, rankings, search_pansou, search_tmdb, send_telegram, tmdb_details
from .magnet import MagnetService
from .models import (
    CreateFolderRequest,
    CredentialRequest,
    DirectoryRequest,
    IntegrationSettingsRequest,
    KeepTemporaryRequest,
    LoginRequest,
    MagnetInspectRequest,
    MagnetPrepareRequest,
    PreparePlayRequest,
    ProviderName,
    ResourceInspectRequest,
    SubscriptionRequest,
    TransferRequest,
)
from .providers.auth import ProviderAuthError, poll_115_qr, start_115_qr
from .providers.base import AuthenticationError, CapabilityError, CloudError, ShareFile, credential_payload
from .providers.registry import ProviderRegistry
from .providers.quark import QuarkAdapter
from .providers.quark_tv import poll_quark_tv_qr, start_quark_tv_qr
from .storage import Store, utc_now
from .vault import CredentialVault


settings = get_settings()
store = Store(settings.database_path)
vault = CredentialVault(settings.data_dir)
STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "feihai_admin"
SESSION_SECONDS = 7 * 24 * 60 * 60
_qr_sessions: dict[str, dict[str, Any]] = {}
_inspect_cache: dict[str, tuple[float, Any]] = {}
_play_rate: dict[str, deque[float]] = defaultdict(deque)
_magnet_tasks: dict[str, asyncio.Task[None]] = {}
_cloud_play_tasks: dict[str, asyncio.Task[None]] = {}
magnet_service = MagnetService(settings.data_dir, settings.magnet_max_bytes)


def _safe_equal(left: str, right: str) -> bool:
    """Constant-time comparison that also supports Chinese credentials."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _integration_values() -> dict[str, str]:
    saved = store.settings()
    tmdb_key = vault.load("tmdb_api_key") or settings.tmdb_api_key
    if tmdb_key.strip().lower() in {"change-me-now", "password", "your-api-key", "请填写"}:
        tmdb_key = ""
    return {
        "pansou_url": str(saved.get("pansou_url") or settings.pansou_url),
        "checker_url": str(saved.get("checker_url") or settings.checker_url),
        "tmdb_api_key": tmdb_key,
        "telegram_bot_token": vault.load("telegram_bot_token") or settings.telegram_bot_token,
        "telegram_chat_id": vault.load("telegram_chat_id") or settings.telegram_chat_id,
        "pansou_token": vault.load("pansou_token"),
        "checker_token": vault.load("checker_token"),
    }


def _session_token(username: str) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    payload = f"{username}:{expires}"
    key = hashlib.sha256(settings.admin_password.encode()).digest()
    signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode().rstrip("=")


def _session_user(token: str | None) -> str | None:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        username, expires, signature = decoded.rsplit(":", 2)
        payload = f"{username}:{expires}"
        key = hashlib.sha256(settings.admin_password.encode()).digest()
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires) < int(time.time()):
            return None
        if not _safe_equal(username, settings.admin_username):
            return None
        return username
    except (ValueError, UnicodeError):
        return None


def require_admin(request: Request) -> str:
    user = _session_user(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录管理员账号")
    return user


def _adapter(provider: str):
    return ProviderRegistry.create(provider, vault.load(f"provider_{provider}"))


def _cloud_error(error: Exception) -> HTTPException:
    if isinstance(error, AuthenticationError):
        return HTTPException(status_code=401, detail=str(error))
    if isinstance(error, CapabilityError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, CloudError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail="操作失败，请查看任务记录")


async def _cleanup_expired() -> None:
    await asyncio.sleep(5)
    while True:
        for item in store.expired_temps(utc_now()):
            try:
                if item["provider"] == "magnet":
                    magnet_service.safe_remove(item["direct_hint"].get("local_path", ""))
                else:
                    adapter = _adapter(item["provider"])
                    await adapter.delete([item["cloud_file_id"]], [item["direct_hint"].get("path", "")])
                store.set_temp_state(item["id"], "deleted")
                store.add_history("temp_cleanup", item["provider"], f"已清理 {item['file_name']}")
            except Exception as error:
                store.set_temp_state(item["id"], "cleanup_failed")
                store.add_history("temp_cleanup_failed", item["provider"], str(error)[:300])
        await asyncio.sleep(settings.cleanup_interval_seconds)


async def _monitor_accounts() -> None:
    """Low-frequency credential check; warnings are sent only when the state changes."""
    await asyncio.sleep(60)
    while True:
        integration = _integration_values()
        for provider in ("baidu", "quark", "115", "china_mobile"):
            if not vault.configured(f"provider_{provider}"):
                continue
            previous = next((item for item in store.accounts() if item["provider"] == provider), {})
            try:
                data = await _adapter(provider).probe()
                store.update_account(provider, state="connected", risk_status="normal", last_error="", account_label=str(data.get("account") or previous.get("account_label") or "已授权"))
            except Exception as error:
                message = str(error)[:300]
                store.update_account(provider, state="error", risk_status="warning", last_error=message)
                warning_key = f"account_warning_{provider}"
                if store.settings().get(warning_key) != message:
                    await send_telegram(integration["telegram_bot_token"], integration["telegram_chat_id"], f"飞海网盘提醒：{ProviderRegistry.labels[provider]} 授权或连接异常\n{message}")
                    store.save_settings({warning_key: message})
        await asyncio.sleep(6 * 60 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    vault.initialize()
    cleanup = asyncio.create_task(_cleanup_expired())
    monitor = asyncio.create_task(_monitor_accounts())
    try:
        yield
    finally:
        cleanup.cancel()
        monitor.cancel()
        background_tasks = [*_cloud_play_tasks.values(), *_magnet_tasks.values()]
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(cleanup, monitor, *background_tasks, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    # 网盘云端转码 CDN（尤其夸克）会拒绝带 NAS 页面来源的请求。
    # 从文档入口统一禁用 Referer，确保浏览器后续的视频重定向请求不被 ACL 拦截。
    return FileResponse(STATIC_DIR / "index.html", headers={"Referrer-Policy": "no-referrer"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "name": settings.app_name,
        "version": "1.0.19",
        "port_policy": "single-port",
        "temp_retention_hours": settings.temp_retention_hours,
        "magnet_playback": True,
        "magnet_max_gb": round(settings.magnet_max_bytes / 1024 / 1024 / 1024),
    }


@app.get("/api/session")
def session(request: Request) -> dict[str, Any]:
    user = _session_user(request.cookies.get(SESSION_COOKIE))
    return {"is_admin": bool(user), "username": user or "访客"}


@app.post("/api/login")
def login(payload: LoginRequest) -> JSONResponse:
    if not (
        _safe_equal(payload.username, settings.admin_username)
        and _safe_equal(payload.password, settings.admin_password)
    ):
        raise HTTPException(status_code=401, detail="管理员账号或密码不正确")
    response = JSONResponse({"ok": True, "username": payload.username})
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(payload.username),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/rankings")
async def ranking_endpoint(
    media_type: str = Query("all", pattern=r"^(all|movie|tv)$"),
    page: int = Query(1, ge=1, le=500),
    year: int | None = Query(None, ge=1900, le=2200),
    genre: str = Query("", max_length=30),
    country: str = Query("", max_length=10),
) -> dict[str, Any]:
    try:
        return await rankings(_integration_values()["tmdb_api_key"], media_type, page, year, genre, country)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"TMDB 暂时无法访问：{type(error).__name__}") from error


@app.get("/api/media/{media_type}/{media_id}")
async def media_details_endpoint(
    media_type: str,
    media_id: int,
) -> dict[str, Any]:
    if media_type not in {"movie", "tv"} or media_id < 1:
        raise HTTPException(status_code=400, detail="影视编号或类型不正确")
    try:
        return await tmdb_details(_integration_values()["tmdb_api_key"], media_type, media_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"TMDB 详情暂时无法访问：{type(error).__name__}") from error


def _resource_match(item: dict[str, Any], media: dict[str, Any] | None, query: str) -> tuple[int, list[str]]:
    if not media:
        return (20 if item.get("recognized") else 0), ["未选择影视版本"]
    title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(item.get("title") or "").casefold())
    wanted = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(media.get("title") or query).casefold())
    score, reasons = 0, []
    if wanted and wanted in title:
        score += 55
        reasons.append("片名匹配")
    elif item.get("recognized"):
        score += 30
        reasons.append("关键词相关")
    year = str(media.get("year") or "")
    if year and year in str(item.get("title") or ""):
        score += 20
        reasons.append("年份匹配")
    is_series = bool(item.get("episode") or re.search(r"(?:S\d+E\d+|第\s*\d+\s*集|更新至?\s*\d+)", str(item.get("title") or ""), re.I))
    if (media.get("media_type") == "tv") == is_series:
        score += 15
        reasons.append("影视类型匹配")
    if item.get("validation_state") == "valid":
        score += 10
        reasons.append("链接已验证")
    return min(score, 100), reasons


@app.get("/api/search")
async def search_endpoint(
    q: str = Query(min_length=1, max_length=200),
    tmdb_id: int | None = Query(None, ge=1),
    media_type: str = Query("", pattern=r"^(|movie|tv)$"),
) -> dict[str, Any]:
    integration = _integration_values()
    try:
        resources, media_result = await asyncio.gather(
            search_pansou(integration["pansou_url"], q, integration["pansou_token"]),
            search_tmdb(integration["tmdb_api_key"], q),
            return_exceptions=True,
        )
    except (ValueError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if isinstance(resources, Exception):
        raise HTTPException(status_code=502, detail=str(resources))
    media = [] if isinstance(media_result, Exception) else media_result
    checked = await check_links(
        integration["checker_url"],
        [item["url"] for item in resources],
        integration["checker_token"],
    )
    visible = []
    selected_media = next((value for value in media if value.get("id") == tmdb_id and (not media_type or value.get("media_type") == media_type)), None)
    selected_media = selected_media or (media[0] if media and tmdb_id is None else None)
    for item in resources:
        state = checked.get(item["url"], {"state": "unverifiable", "reason": "保留显示"})
        if state["state"] == "invalid":
            continue
        item["validation_state"] = state["state"]
        item["validation_reason"] = state["reason"]
        item["poster"] = selected_media.get("poster", "") if selected_media else ""
        item["overview"] = selected_media.get("overview", "暂无简介") if selected_media else "暂无简介"
        item["media"] = selected_media
        item["match_score"], item["match_reasons"] = _resource_match(item, selected_media, q)
        visible.append(item)
    visible.sort(key=lambda value: (value.get("match_score", 0), value.get("validation_state") == "valid", value.get("episode", 0)), reverse=True)
    return {
        "query": q,
        "media": media,
        "selected_media": selected_media,
        "resources": visible,
        "found": len(resources),
        "hidden_invalid": len(resources) - len(visible),
        "rule": "仅隐藏检测网站明确判定失效的链接",
    }


async def _inspect(payload: ResourceInspectRequest):
    url = str(payload.share_url)
    cache_key = hashlib.sha256(f"{payload.provider}:{url}:{payload.extraction_code}".encode()).hexdigest()
    cached = _inspect_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]
    adapter = _adapter(payload.provider.value)
    inspection = await adapter.inspect_share(url, payload.extraction_code)
    _inspect_cache[cache_key] = (time.time() + 300, inspection)
    return inspection


@app.post("/api/resources/inspect")
async def inspect_resource(payload: ResourceInspectRequest) -> dict[str, Any]:
    try:
        inspection = await _inspect(payload)
        return inspection.public()
    except Exception as error:
        raise _cloud_error(error) from error


@app.post("/api/magnet/inspect")
async def inspect_magnet(payload: MagnetInspectRequest) -> dict[str, Any]:
    try:
        key = hashlib.sha256(payload.magnet_url.encode()).hexdigest()
        cached = _inspect_cache.get(key)
        inspection = cached[1] if cached and cached[0] > time.time() else await magnet_service.inspect(payload.magnet_url)
        _inspect_cache[key] = (time.time() + 3600, inspection)
        return inspection.public()
    except Exception as error:
        raise _cloud_error(error) from error


def _limit_play(request: Request) -> None:
    key = request.client.host if request.client else "local"
    bucket = _play_rate[key]
    cutoff = time.time() - 120
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= 20:
        raise HTTPException(status_code=429, detail="短时间准备了太多不同资源，请稍候两分钟再试")
    bucket.append(time.time())


def _file_hint(file: ShareFile, source_file_id: str = "") -> dict[str, Any]:
    return {
        "id": file.id,
        "name": file.name,
        "parent_id": file.parent_id,
        "mime_type": file.mime_type,
        "pick_code": file.pick_code,
        "path": file.path,
        "source_file_id": source_file_id,
    }


def _temp_file(item: dict[str, Any]) -> ShareFile:
    hint = item["direct_hint"]
    return ShareFile(
        id=item["cloud_file_id"],
        name=item["file_name"],
        size=int(item.get("size") or 0),
        parent_id=item.get("cloud_parent_id") or hint.get("parent_id", ""),
        mime_type=item.get("mime_type") or hint.get("mime_type", ""),
        pick_code=hint.get("pick_code", ""),
        path=hint.get("path", ""),
    )


def _temporary_play_url(item: dict[str, Any]) -> str:
    if item.get("provider") == "115":
        return f"/api/hls/{item['id']}/master.m3u8"
    return f"/api/play/{item['id']}"


def _cloud_temp_item(temp_id: str, payload: PreparePlayRequest, selected: ShareFile, state: str, hint: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": temp_id, "provider": payload.provider.value, "title": payload.title,
        "share_url": str(payload.share_url), "extraction_code": payload.extraction_code,
        "cloud_file_id": selected.id, "cloud_parent_id": selected.parent_id,
        "file_name": selected.name, "mime_type": selected.mime_type, "size": selected.size,
        "direct_hint": hint, "last_played_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=settings.temp_retention_hours)).isoformat(),
        "state": state, "created_at": now.isoformat(),
    }


async def _prepare_cloud_temp(temp_id: str, payload: PreparePlayRequest, inspection: Any, selected: ShareFile) -> None:
    try:
        adapter = _adapter(payload.provider.value)
        store.update_temp(temp_id, direct_hint={"source_file_id": selected.id, "progress": 15, "stage": "正在创建临时播放目录"})
        folder = await adapter.ensure_folder(adapter.root_id, "/", settings.temp_folder_name)
        store.update_temp(temp_id, direct_hint={"source_file_id": selected.id, "progress": 35, "stage": "正在同盘保存所选视频"})
        saved = await adapter.save_share(inspection, folder.id, folder.path, [selected.id], "skip")
        saved_file = next((item for item in saved.saved_files if item.name == selected.name), None)
        if not saved_file:
            store.update_temp(temp_id, direct_hint={"source_file_id": selected.id, "progress": 70, "stage": "网盘已接受，正在定位视频"})
            for attempt in range(4):
                located = await adapter.locate_saved_files(folder.id, folder.path, [selected.name])
                saved_file = located[0] if located else None
                if saved_file:
                    break
                await asyncio.sleep(2 + attempt * 2)
        if not saved_file:
            raise CloudError("网盘已接受临时保存，但暂时找不到视频文件，请稍后重试")
        now = datetime.now(UTC)
        store.add_temp({
            "id": temp_id, "provider": payload.provider.value, "title": payload.title,
            "share_url": str(payload.share_url), "extraction_code": payload.extraction_code,
            "cloud_file_id": saved_file.id, "cloud_parent_id": saved_file.parent_id or folder.id,
            "file_name": saved_file.name, "mime_type": saved_file.mime_type, "size": saved_file.size,
            "direct_hint": {**_file_hint(saved_file, selected.id), "progress": 100, "stage": "视频已准备完成"},
            "last_played_at": now.isoformat(), "expires_at": (now + timedelta(hours=settings.temp_retention_hours)).isoformat(),
            "state": "ready", "created_at": now.isoformat(),
        })
        store.add_history("prepare_play", payload.provider.value, f"临时保存 {saved_file.name}")
    except asyncio.CancelledError:
        store.update_temp(temp_id, state="canceled", direct_hint={"source_file_id": selected.id, "progress": 0, "stage": "已取消"})
        raise
    except Exception as error:
        store.update_temp(temp_id, state="failed", direct_hint={"source_file_id": selected.id, "progress": 0, "stage": "准备失败", "error": str(error)[:500]})


@app.post("/api/play/prepare")
async def prepare_play(payload: PreparePlayRequest, request: Request) -> dict[str, Any]:
    try:
        if payload.provider.value == "115":
            raise CapabilityError("115 已关闭在线播放，可复制分享链接或使用同盘保存")
        inspection = await _inspect(payload)
        candidates = [item for item in inspection.files if not item.is_dir and item.browser.playable]
        selected = next((item for item in candidates if item.id == payload.file_id), None) if payload.file_id else None
        selected = selected or (candidates[0] if candidates else None)
        if not selected:
            raise CapabilityError("这个资源没有确认适合网页播放的 MP4/H.264/AAC 或 WebM 文件")
        existing = store.find_ready_temp(payload.provider.value, str(payload.share_url), selected.name)
        if existing:
            return {"temp_id": existing["id"], "state": "ready", "play_url": _temporary_play_url(existing), "status_url": f"/api/play/status/{existing['id']}", "reused": True}
        pending = store.find_temp(payload.provider.value, str(payload.share_url), selected.name)
        if pending:
            return {"temp_id": pending["id"], "state": "preparing", "play_url": "", "status_url": f"/api/play/status/{pending['id']}", "reused": True}
        _limit_play(request)
        temp_id = uuid.uuid4().hex
        store.add_temp(_cloud_temp_item(temp_id, payload, selected, "preparing", {"source_file_id": selected.id, "progress": 5, "stage": "任务已创建"}))
        task = asyncio.create_task(_prepare_cloud_temp(temp_id, payload, inspection, selected))
        _cloud_play_tasks[temp_id] = task
        task.add_done_callback(lambda _: _cloud_play_tasks.pop(temp_id, None))
        return {"temp_id": temp_id, "state": "preparing", "play_url": "", "status_url": f"/api/play/status/{temp_id}", "reused": False}
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/play/status/{temp_id}")
def cloud_play_status(temp_id: str) -> dict[str, Any]:
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="播放准备任务不存在") from error
    hint = item["direct_hint"]
    return {
        "temp_id": temp_id, "state": item["state"], "progress": int(hint.get("progress") or 0),
        "message": hint.get("error") or hint.get("stage") or "正在准备视频",
        "play_url": _temporary_play_url(item) if item["state"] == "ready" else "",
    }


@app.delete("/api/play/status/{temp_id}")
async def cancel_cloud_play(temp_id: str) -> dict[str, Any]:
    task = _cloud_play_tasks.get(temp_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        item = store.temp(temp_id)
        if item["state"] == "preparing":
            store.update_temp(temp_id, state="canceled", direct_hint={**item["direct_hint"], "progress": 0, "stage": "已取消"})
    except KeyError:
        pass
    return {"ok": True}


def _magnet_temp_item(
    temp_id: str,
    payload: MagnetPrepareRequest,
    file: ShareFile,
    state: str,
    now: datetime,
    direct_hint: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": temp_id,
        "provider": "magnet",
        "title": payload.title,
        "share_url": payload.magnet_url,
        "extraction_code": "",
        "cloud_file_id": temp_id,
        "cloud_parent_id": "",
        "file_name": file.name,
        "mime_type": file.mime_type,
        "size": file.size,
        "direct_hint": direct_hint,
        "last_played_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=settings.temp_retention_hours)).isoformat(),
        "state": state,
        "created_at": now.isoformat(),
    }


async def _download_magnet(temp_id: str, payload: MagnetPrepareRequest, selected: ShareFile) -> None:
    now = datetime.now(UTC)
    try:
        local_path, downloaded = await magnet_service.download(
            payload.magnet_url,
            payload.file_id,
            magnet_service.cache_dir / temp_id,
        )
        support = await magnet_service.probe_codecs(local_path)
        if not support.playable:
            raise CapabilityError(support.reason)
        mime_type = "video/webm" if local_path.suffix.lower() == ".webm" else "video/mp4"
        downloaded.mime_type = mime_type
        store.add_temp(_magnet_temp_item(
            temp_id,
            payload,
            downloaded,
            "ready",
            now,
            {"local_path": str(local_path), "source_file_id": payload.file_id, "format_reason": support.reason},
        ))
        store.add_history("prepare_play", "magnet", f"磁力临时缓存 {downloaded.name}")
    except Exception as error:
        store.add_temp(_magnet_temp_item(
            temp_id,
            payload,
            selected,
            "failed",
            now,
            {"error": str(error)[:500], "source_file_id": payload.file_id},
        ))


@app.post("/api/magnet/prepare")
async def prepare_magnet(payload: MagnetPrepareRequest, request: Request) -> dict[str, Any]:
    try:
        inspection = await magnet_service.inspect(payload.magnet_url)
        selected = next((item for item in inspection.files if item.id == payload.file_id), None)
        if not selected or not selected.browser.playable:
            raise CapabilityError("这个磁力资源没有确认适合网页播放的视频")
        existing = store.find_ready_temp("magnet", payload.magnet_url, selected.name)
        if existing and Path(existing["direct_hint"].get("local_path", "")).is_file():
            return {
                "temp_id": existing["id"],
                "state": "ready",
                "play_url": f"/api/play/{existing['id']}",
                "status_url": f"/api/magnet/status/{existing['id']}",
                "reused": True,
            }
        _limit_play(request)
        temp_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        store.add_temp(_magnet_temp_item(
            temp_id, payload, selected, "preparing", now,
            {"source_file_id": payload.file_id, "expected_size": selected.size},
        ))
        task = asyncio.create_task(_download_magnet(temp_id, payload, selected))
        _magnet_tasks[temp_id] = task
        task.add_done_callback(lambda _: _magnet_tasks.pop(temp_id, None))
        return {
            "temp_id": temp_id,
            "state": "preparing",
            "play_url": f"/api/play/{temp_id}",
            "status_url": f"/api/magnet/status/{temp_id}",
            "reused": False,
        }
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/magnet/status/{temp_id}")
def magnet_status(temp_id: str) -> dict[str, Any]:
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="磁力播放任务不存在") from error
    if item["provider"] != "magnet":
        raise HTTPException(status_code=400, detail="不是磁力播放任务")
    return {
        "temp_id": temp_id,
        "state": item["state"],
        "message": item["direct_hint"].get("error") or (
            "视频已准备完成" if item["state"] == "ready" else "正在从磁力节点获取所选视频"
        ),
        "play_url": f"/api/play/{temp_id}" if item["state"] == "ready" else "",
    }


def _hls_asset_token(url: str) -> str:
    payload = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    signature = hmac.new(settings.admin_password.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def _hls_asset_url(temp_id: str, url: str) -> str:
    return f"/api/hls/{temp_id}/asset/{_hls_asset_token(url)}"


def _decode_hls_asset(token: str) -> str:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(settings.admin_password.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        padded = payload + "=" * (-len(payload) % 4)
        url = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="播放片段地址无效") from error
    if not url.lower().startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="播放片段地址无效")
    return url


def _rewrite_hls_playlist(temp_id: str, text: str, base_url: str) -> str:
    def localize(value: str) -> str:
        if not value or value.startswith("data:"):
            return value
        return _hls_asset_url(temp_id, urljoin(base_url, value))

    output: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            output.append(localize(line))
            continue
        output.append(re.sub(r'URI="([^"]+)"', lambda match: f'URI="{localize(match.group(1))}"', raw_line))
    return "\n".join(output) + "\n"


async def _proxy_115_hls(temp_id: str, item: dict[str, Any], url: str, request: Request) -> Response:
    adapter = _adapter("115")
    direct = await adapter.direct_link(_temp_file(item))
    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    headers = dict(direct.headers)
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise CloudError(f"115播放线路返回 HTTP {upstream.status_code}")
    content_type = upstream.headers.get("content-type", "").lower()
    resolved_url = str(upstream.url)
    is_playlist = "mpegurl" in content_type or resolved_url.lower().split("?", 1)[0].endswith(".m3u8")
    if is_playlist:
        content = (await upstream.aread()).decode("utf-8", errors="replace")
        await upstream.aclose()
        await client.aclose()
        return Response(
            _rewrite_hls_playlist(temp_id, content, resolved_url),
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "private, no-store", "X-Feihai-Playback": "115-hls"},
        )

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes(1024 * 512):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in {"content-length", "content-range", "accept-ranges", "etag", "last-modified"}
    }
    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type") or "video/mp2t",
        headers=response_headers,
    )


@app.get("/api/hls/{temp_id}/master.m3u8")
async def hls_master(temp_id: str, request: Request) -> Response:
    raise HTTPException(status_code=410, detail="115 已关闭在线播放")
    # 保留下面的旧临时记录兼容代码，当前不会进入。
    try:
        item = store.temp(temp_id)
        if item["provider"] != "115" or item["state"] != "ready":
            raise HTTPException(status_code=400, detail="这不是可用的115临时播放文件")
        now = utc_now()
        store.touch_temp(
            temp_id,
            now.isoformat(),
            (now + timedelta(hours=settings.temp_retention_hours)).isoformat(),
        )
        direct = await _adapter("115").direct_link(_temp_file(item))
        return await _proxy_115_hls(temp_id, item, direct.url, request)
    except HTTPException:
        raise
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/hls/{temp_id}/asset/{token}")
async def hls_asset(temp_id: str, token: str, request: Request) -> Response:
    raise HTTPException(status_code=410, detail="115 已关闭在线播放")
    try:
        item = store.temp(temp_id)
        if item["provider"] != "115" or item["state"] != "ready":
            raise HTTPException(status_code=400, detail="这不是可用的115临时播放文件")
        return await _proxy_115_hls(temp_id, item, _decode_hls_asset(token), request)
    except HTTPException:
        raise
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/play/{temp_id}")
async def stream_temp(temp_id: str, request: Request) -> Response:
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="临时播放文件不存在") from error
    if item["state"] != "ready":
        if item["state"] == "preparing":
            raise HTTPException(status_code=425, detail="视频仍在准备中")
        raise HTTPException(status_code=410, detail=item["direct_hint"].get("error") or "临时播放文件已经清理")
    if item["provider"] == "115":
        raise HTTPException(status_code=410, detail="115 已关闭在线播放")
    now = datetime.now(UTC)
    store.touch_temp(temp_id, now.isoformat(), (now + timedelta(hours=settings.temp_retention_hours)).isoformat())
    if item["provider"] == "magnet":
        local_path = Path(item["direct_hint"].get("local_path", ""))
        try:
            resolved = local_path.resolve()
            root = magnet_service.cache_dir.resolve()
            if not resolved.is_file() or root not in resolved.parents:
                raise FileNotFoundError
        except (OSError, FileNotFoundError) as error:
            raise HTTPException(status_code=410, detail="磁力临时视频已不存在") from error
        return FileResponse(resolved, media_type=item["mime_type"] or "video/mp4")
    try:
        direct = await _adapter(item["provider"]).direct_link(_temp_file(item))
        if direct.redirect:
            return RedirectResponse(
                direct.url,
                status_code=302,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Feihai-Playback": "cloud-transcode",
                    # Quark's cloud-transcode CDN rejects media subrequests
                    # whose Referer points at the local NAS page.  Applying the
                    # policy on the redirect response makes the browser omit it
                    # while keeping the fast CDN-to-browser playback path.
                    "Referrer-Policy": "no-referrer",
                },
            )
        client = httpx.AsyncClient(timeout=None, follow_redirects=True)
        upstream_headers = dict(direct.headers)
        if request.headers.get("range"):
            upstream_headers["Range"] = request.headers["range"]
        upstream_request = client.build_request("GET", direct.url, headers=upstream_headers)
        response = await client.send(upstream_request, stream=True)
        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise CloudError(f"网盘播放地址返回 HTTP {response.status_code}")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes(1024 * 512):
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-length", "content-range", "accept-ranges", "etag", "last-modified"}
        }
        return StreamingResponse(
            chunks(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type") or direct.mime_type,
            headers=headers,
        )
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/admin/overview")
def admin_overview(_: str = Depends(require_admin)) -> dict[str, Any]:
    accounts = {item["provider"]: item for item in store.accounts()}
    last_dirs = store.last_directories()
    return {
        "accounts": [
            {**accounts[name], "label": ProviderRegistry.labels[name], "configured": vault.configured(f"provider_{name}"), "last_directory": last_dirs.get(name)}
            for name in ("baidu", "quark", "115", "china_mobile")
        ],
        "jobs": store.jobs(),
        "temporary": store.temps(),
        "subscriptions": store.subscriptions(),
        "history": store.history(30),
    }


@app.get("/api/admin/backup")
def export_backup(_: str = Depends(require_admin)) -> Response:
    vault.initialize()
    credentials = {
        path.name: base64.b64encode(path.read_bytes()).decode()
        for path in vault.secret_dir.glob("*.token") if path.is_file()
    }
    payload = {
        "format": "feihai-portable-backup", "version": 1, "created_at": utc_now(),
        "database": store.export_portable(), "credentials": credentials,
        "credential_key": base64.b64encode(vault.key_path.read_bytes()).decode(),
    }
    filename = f"feihai-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@app.post("/api/admin/backup/restore")
async def restore_backup(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="备份文件过大")
    try:
        payload = json.loads(raw)
        if payload.get("format") != "feihai-portable-backup" or int(payload.get("version") or 0) != 1:
            raise ValueError
        key = base64.b64decode(payload["credential_key"], validate=True)
        credentials = payload.get("credentials") or {}
        if len(key) < 32 or not isinstance(credentials, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="不是有效的飞海网盘备份文件") from error
    vault.initialize()
    vault.key_path.write_bytes(key)
    for name, value in credentials.items():
        if re.fullmatch(r"[a-zA-Z0-9_.-]+\.token", str(name)):
            try:
                (vault.secret_dir / str(name)).write_bytes(base64.b64decode(value, validate=True))
            except (ValueError, TypeError):
                continue
    store.restore_portable(payload.get("database") or {})
    store.add_history("restore_backup", "", "已恢复系统设置、追更、目录与加密凭证")
    return {"ok": True, "message": "备份已恢复，请重新测试各网盘连接"}


@app.post("/api/admin/accounts/check-all")
async def check_all_accounts(_: str = Depends(require_admin)) -> dict[str, Any]:
    results = []
    for provider in ("baidu", "quark", "115", "china_mobile"):
        if not vault.configured(f"provider_{provider}"):
            results.append({"provider": provider, "state": "not_configured"})
            continue
        try:
            data = await _adapter(provider).probe()
            store.update_account(provider, state="connected", risk_status="normal", last_error="", account_label=str(data.get("account") or "已授权"))
            results.append({"provider": provider, "state": "connected"})
        except Exception as error:
            store.update_account(provider, state="error", risk_status="warning", last_error=str(error)[:300])
            results.append({"provider": provider, "state": "error", "message": str(error)[:300]})
    return {"results": results}


@app.put("/api/admin/accounts/{provider}")
async def save_account(provider: ProviderName, payload: CredentialRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    name = provider.value
    try:
        credential = payload.credential
        if name == "quark":
            incoming = credential_payload(payload.credential)
            if "cookie" not in incoming:
                incoming = {"cookie": incoming.get("credential", payload.credential)}
            existing_raw = vault.load("provider_quark")
            existing = credential_payload(existing_raw) if existing_raw else {}
            for key in ("tv_refresh_token", "tv_device_id"):
                if existing.get(key) and not incoming.get(key):
                    incoming[key] = existing[key]
            credential = json.dumps(incoming, ensure_ascii=False)
        adapter = ProviderRegistry.create(name, credential)
        account = await adapter.probe()
        vault.save(f"provider_{name}", credential)
        return store.update_account(
            name,
            state="connected",
            account_label=str(account.get("account") or payload.account_label),
            credential_kind=payload.kind,
            risk_status="normal",
            last_error="",
        )
    except Exception as error:
        store.update_account(name, state="error", last_error=str(error)[:300], risk_status="warning")
        raise _cloud_error(error) from error


@app.delete("/api/admin/accounts/{provider}")
def remove_account(provider: ProviderName, _: str = Depends(require_admin)) -> dict[str, Any]:
    vault.delete(f"provider_{provider.value}")
    store.update_account(provider.value, state="disconnected", account_label="", credential_kind="", risk_status="unknown", last_error="")
    return {"ok": True}


@app.post("/api/admin/accounts/{provider}/probe")
async def probe_account(provider: ProviderName, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        data = await _adapter(provider.value).probe()
        store.update_account(provider.value, state="connected", risk_status="normal", last_error="", account_label=str(data.get("account") or "已授权"))
        return {"ok": True, **data}
    except Exception as error:
        store.update_account(provider.value, state="error", risk_status="warning", last_error=str(error)[:300])
        raise _cloud_error(error) from error


@app.post("/api/admin/accounts/115/qr/start")
async def qr_start(_: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        public, secret_value = await start_115_qr()
    except (ProviderAuthError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    session_id = uuid.uuid4().hex
    _qr_sessions[session_id] = {"expires": time.time() + 300, "secret": secret_value}
    return {"session_id": session_id, **public}


@app.get("/api/admin/accounts/115/qr/{session_id}")
async def qr_poll(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    session_value = _qr_sessions.get(session_id)
    if not session_value or session_value["expires"] < time.time():
        raise HTTPException(status_code=410, detail="二维码已过期，请刷新")
    try:
        state, message, credential = await poll_115_qr(session_value["secret"])
        if credential:
            adapter = ProviderRegistry.create("115", credential)
            account = await adapter.probe()
            vault.save("provider_115", credential)
            store.update_account("115", state="connected", account_label=str(account.get("account") or "115账号"), credential_kind="qr", risk_status="normal", last_error="")
            _qr_sessions.pop(session_id, None)
        return {"state": state, "message": message}
    except Exception as error:
        raise _cloud_error(error) from error


@app.post("/api/admin/accounts/quark/tv/qr/start")
async def quark_tv_qr_start(_: str = Depends(require_admin)) -> dict[str, Any]:
    if not vault.configured("provider_quark"):
        raise HTTPException(status_code=400, detail="请先保存夸克 Cookie，再绑定电视端播放授权")
    try:
        public, secret_value = await start_quark_tv_qr()
    except (AuthenticationError, CloudError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    session_id = uuid.uuid4().hex
    _qr_sessions[session_id] = {"expires": time.time() + 300, "secret": secret_value}
    return {"session_id": session_id, **public}


@app.get("/api/admin/accounts/quark/tv/qr/{session_id}")
async def quark_tv_qr_poll(session_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    session_value = _qr_sessions.get(session_id)
    if not session_value or session_value["expires"] < time.time():
        raise HTTPException(status_code=410, detail="二维码已过期，请刷新")
    try:
        state, message, tokens = await poll_quark_tv_qr(session_value["secret"])
        if tokens:
            current_raw = vault.load("provider_quark")
            current = credential_payload(current_raw)
            if "cookie" not in current:
                current = {"cookie": current.get("credential", current_raw)}
            current.update(tokens)
            credential = json.dumps(current, ensure_ascii=False)
            adapter = ProviderRegistry.create("quark", credential)
            account = await adapter.probe()
            vault.save("provider_quark", credential)
            store.update_account(
                "quark",
                state="connected",
                account_label=str(account.get("account") or "夸克账号"),
                credential_kind="Cookie + 电视端扫码",
                risk_status="normal",
                last_error="",
            )
            _qr_sessions.pop(session_id, None)
        return {"state": state, "message": message}
    except Exception as error:
        raise _cloud_error(error) from error


@app.post("/api/admin/accounts/{provider}/directories")
async def directories(provider: ProviderName, payload: DirectoryRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        items = await _adapter(provider.value).list_directories(payload.parent_id, payload.parent_path)
        return {"items": [item.__dict__ if hasattr(item, "__dict__") else {"id": item.id, "name": item.name, "path": item.path} for item in items]}
    except Exception as error:
        raise _cloud_error(error) from error


@app.post("/api/admin/accounts/{provider}/folders")
async def create_folder(provider: ProviderName, payload: CreateFolderRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        item = await _adapter(provider.value).create_folder(payload.parent_id, payload.parent_path, payload.name)
        return {"id": item.id, "name": item.name, "path": item.path}
    except Exception as error:
        raise _cloud_error(error) from error


async def _run_save_job(job_id: int, payload: TransferRequest) -> None:
    try:
        store.update_job(job_id, status="running", progress=10, stage="正在读取分享内容")
        inspection = await _inspect(payload)
        adapter = _adapter(payload.provider.value)
        selected = [item for item in inspection.files if item.id in set(payload.selected_file_ids)]
        names = [item.name for item in selected] if selected else [item.name for item in inspection.files if not item.is_dir]
        # A blank selection means "save the original share as-is". Do not
        # enumerate and compare every nested file before that operation; the
        # provider handles whole-share duplicates more reliably and quickly.
        if payload.selected_file_ids and names:
            store.update_job(job_id, progress=30, stage="正在检查目标目录")
            existing = await adapter.locate_saved_files(payload.target_id, payload.target_path, names)
            if existing and payload.duplicate_policy == "skip":
                store.update_job(job_id, status="success", progress=100, stage="同名内容已存在，已跳过", detail={"duplicate": True, "files": [item.name for item in existing]})
                return
        store.update_job(job_id, progress=45, stage="正在同网盘保存")
        result = await adapter.save_share(inspection, payload.target_id, payload.target_path, payload.selected_file_ids, payload.duplicate_policy)
        store.save_last_directory(payload.provider.value, payload.target_id, payload.target_path)
        detail = {"message": result.message, "files": [item.name for item in result.saved_files], "duplicate": result.duplicate}
        store.update_job(job_id, status="success", progress=100, stage="保存完成", detail=detail)
        store.add_history("permanent_save", payload.provider.value, f"{payload.title} → {payload.target_path}")
        integration = _integration_values()
        try:
            await send_telegram(integration["telegram_bot_token"], integration["telegram_chat_id"], f"飞海网盘：{payload.title} 已保存到 {payload.target_path}")
        except Exception:
            pass
    except Exception as error:
        store.update_job(job_id, status="failed", stage="保存失败", error=str(error)[:500])


@app.post("/api/admin/save")
async def permanent_save(payload: TransferRequest, background: BackgroundTasks, _: str = Depends(require_admin)) -> dict[str, Any]:
    detail = payload.model_dump(mode="json")
    job = store.create_job("permanent_save", payload.provider.value, payload.title, detail)
    background.add_task(_run_save_job, job["id"], payload)
    return job


@app.get("/api/admin/jobs")
def jobs(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {"items": store.jobs()}


@app.get("/api/admin/temporary")
def temporary(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {"items": store.temps()}


@app.get("/api/admin/temporary/{temp_id}/playback-diagnostics")
async def playback_diagnostics(temp_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Return sanitized preview metadata without exposing signed URLs or credentials."""
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="临时播放文件不存在") from error
    if item["provider"] != "quark":
        return {"provider": item["provider"], "mode": "original"}
    try:
        adapter = _adapter("quark")
        if not isinstance(adapter, QuarkAdapter):
            raise CloudError("夸克适配器不可用")
        preview = await adapter.request(
            "POST",
            f"{adapter.transcode_api}/file/v2/play/project",
            json={
                "fid": item["cloud_file_id"],
                "resolutions": "low,normal,high,super,2k,4k",
                "supports": "fmp4_av,m3u8,dolby_vision",
            },
        )
        data = preview.get("data") if isinstance(preview, dict) else None
        if not isinstance(data, dict):
            return {
                "provider": "quark",
                "response_type": type(preview).__name__,
                "data_type": type(data).__name__,
                "top_keys": sorted(preview.keys()) if isinstance(preview, dict) else [],
            }
        options = []
        for value in data.get("video_list") or []:
            info = value.get("video_info") or {}
            url = str(info.get("url") or "")
            options.append({
                "resolution": info.get("resolution") or value.get("resolution"),
                "format": info.get("format"),
                "hls_type": info.get("hls_type"),
                "codec": info.get("codec"),
                "audio_codec": (info.get("audio") or {}).get("codec"),
                "success": info.get("success"),
                "finish": info.get("finish"),
                "has_url": bool(url),
                "is_hls": ".m3u8" in url.lower(),
                "size": info.get("size"),
                "bitrate": info.get("bitrate"),
            })
        return {"provider": "quark", "options": options}
    except Exception as error:
        return {
            "provider": "quark",
            "error_type": type(error).__name__,
            "error": str(error)[:300],
        }


@app.post("/api/admin/temporary/{temp_id}/keep")
async def keep_temporary(temp_id: str, payload: KeepTemporaryRequest, background: BackgroundTasks, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="临时文件不存在") from error
    transfer = TransferRequest(
        provider=item["provider"],
        share_url=item["share_url"],
        extraction_code=item["extraction_code"],
        title=item["title"],
        target_id=payload.target_id,
        target_path=payload.target_path,
        selected_file_ids=[item["direct_hint"].get("source_file_id", "")],
        duplicate_policy=payload.duplicate_policy,
    )
    job = store.create_job("keep_temporary", item["provider"], item["title"], transfer.model_dump(mode="json"))

    async def run_keep() -> None:
        await _run_save_job(job["id"], transfer)
        current = store.job(job["id"])
        if current["status"] == "success":
            try:
                await _adapter(item["provider"]).delete([item["cloud_file_id"]], [item["direct_hint"].get("path", "")])
                store.set_temp_state(temp_id, "kept")
            except Exception:
                store.set_temp_state(temp_id, "cleanup_failed")

    background.add_task(run_keep)
    return job


@app.get("/api/admin/integrations")
def get_integrations(_: str = Depends(require_admin)) -> dict[str, Any]:
    values = _integration_values()
    return {
        "pansou_url": values["pansou_url"],
        "checker_url": values["checker_url"],
        "tmdb_configured": bool(values["tmdb_api_key"]),
        "telegram_configured": bool(values["telegram_bot_token"] and values["telegram_chat_id"]),
        "tmdb_guide": "https://developer.themoviedb.org/docs/getting-started",
        "rules": {"hide_only_explicit_invalid": True, "single_port": 12366},
    }


@app.put("/api/admin/integrations")
def save_integrations(payload: IntegrationSettingsRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    store.save_settings({"pansou_url": payload.pansou_url.rstrip("/"), "checker_url": payload.checker_url.rstrip("/")})
    if payload.tmdb_api_key:
        vault.save("tmdb_api_key", payload.tmdb_api_key)
    if payload.telegram_bot_token:
        vault.save("telegram_bot_token", payload.telegram_bot_token)
    if payload.telegram_chat_id:
        vault.save("telegram_chat_id", payload.telegram_chat_id)
    return get_integrations(_)


@app.post("/api/admin/subscriptions")
def add_subscription(payload: SubscriptionRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    return store.add_subscription(payload.title, payload.media_type, payload.year)


@app.delete("/api/admin/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: int, _: str = Depends(require_admin)) -> dict[str, Any]:
    store.remove_subscription(subscription_id)
    return {"ok": True}
