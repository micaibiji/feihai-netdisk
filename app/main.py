from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .integrations import check_links, rankings, search_pansou, search_tmdb, send_telegram, tmdb_details
from .models import (
    CreateFolderRequest,
    CredentialRequest,
    DirectoryRequest,
    IntegrationSettingsRequest,
    KeepTemporaryRequest,
    LoginRequest,
    PreparePlayRequest,
    ProviderName,
    ResourceInspectRequest,
    SubscriptionRequest,
    TransferRequest,
)
from .providers.auth import ProviderAuthError, poll_115_qr, start_115_qr
from .providers.base import AuthenticationError, CapabilityError, CloudError, ShareFile
from .providers.registry import ProviderRegistry
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
                adapter = _adapter(item["provider"])
                await adapter.delete([item["cloud_file_id"]], [item["direct_hint"].get("path", "")])
                store.set_temp_state(item["id"], "deleted")
                store.add_history("temp_cleanup", item["provider"], f"已清理 {item['file_name']}")
            except Exception as error:
                store.set_temp_state(item["id"], "cleanup_failed")
                store.add_history("temp_cleanup_failed", item["provider"], str(error)[:300])
        await asyncio.sleep(settings.cleanup_interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.initialize()
    vault.initialize()
    cleanup = asyncio.create_task(_cleanup_expired())
    try:
        yield
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "name": settings.app_name,
        "version": "1.0.4",
        "port_policy": "single-port",
        "temp_retention_hours": settings.temp_retention_hours,
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


@app.get("/api/search")
async def search_endpoint(q: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
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
    best_media = media[0] if media else None
    for item in resources:
        state = checked.get(item["url"], {"state": "unverifiable", "reason": "保留显示"})
        if state["state"] == "invalid":
            continue
        item["validation_state"] = state["state"]
        item["validation_reason"] = state["reason"]
        item["poster"] = best_media.get("poster", "") if best_media else ""
        item["overview"] = best_media.get("overview", "暂无简介") if best_media else "暂无简介"
        item["media"] = best_media
        visible.append(item)
    return {
        "query": q,
        "media": media,
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


def _limit_play(request: Request) -> None:
    key = request.client.host if request.client else "local"
    bucket = _play_rate[key]
    cutoff = time.time() - 600
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= 8:
        raise HTTPException(status_code=429, detail="临时播放准备过于频繁，请十分钟后再试")
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


@app.post("/api/play/prepare")
async def prepare_play(payload: PreparePlayRequest, request: Request) -> dict[str, Any]:
    _limit_play(request)
    try:
        inspection = await _inspect(payload)
        candidates = [item for item in inspection.files if not item.is_dir and item.browser.playable]
        selected = next((item for item in candidates if item.id == payload.file_id), None) if payload.file_id else None
        selected = selected or (candidates[0] if candidates else None)
        if not selected:
            raise CapabilityError("这个资源没有确认适合网页播放的 MP4/H.264/AAC 或 WebM 文件")
        existing = store.find_ready_temp(payload.provider.value, str(payload.share_url), selected.name)
        if existing:
            return {"temp_id": existing["id"], "play_url": f"/api/play/{existing['id']}", "reused": True}
        adapter = _adapter(payload.provider.value)
        folder = await adapter.ensure_folder(adapter.root_id, "/", settings.temp_folder_name)
        saved = await adapter.save_share(inspection, folder.id, folder.path, [selected.id], "skip")
        saved_file = next((item for item in saved.saved_files if item.name == selected.name), None)
        if not saved_file:
            located = await adapter.locate_saved_files(folder.id, folder.path, [selected.name])
            saved_file = located[0] if located else None
        if not saved_file:
            raise CloudError("网盘已接受临时保存，但暂时找不到视频文件，请稍后重试")
        now = datetime.now(UTC)
        temp_id = uuid.uuid4().hex
        store.add_temp({
            "id": temp_id,
            "provider": payload.provider.value,
            "title": payload.title,
            "share_url": str(payload.share_url),
            "extraction_code": payload.extraction_code,
            "cloud_file_id": saved_file.id,
            "cloud_parent_id": saved_file.parent_id or folder.id,
            "file_name": saved_file.name,
            "mime_type": saved_file.mime_type,
            "size": saved_file.size,
            "direct_hint": _file_hint(saved_file, selected.id),
            "last_played_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=settings.temp_retention_hours)).isoformat(),
            "state": "ready",
            "created_at": now.isoformat(),
        })
        store.add_history("prepare_play", payload.provider.value, f"临时保存 {saved_file.name}")
        return {"temp_id": temp_id, "play_url": f"/api/play/{temp_id}", "reused": False}
    except Exception as error:
        raise _cloud_error(error) from error


@app.get("/api/play/{temp_id}")
async def stream_temp(temp_id: str, request: Request) -> StreamingResponse:
    try:
        item = store.temp(temp_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="临时播放文件不存在") from error
    if item["state"] != "ready":
        raise HTTPException(status_code=410, detail="临时播放文件已经清理")
    now = datetime.now(UTC)
    store.touch_temp(temp_id, now.isoformat(), (now + timedelta(hours=settings.temp_retention_hours)).isoformat())
    try:
        direct = await _adapter(item["provider"]).direct_link(_temp_file(item))
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


@app.put("/api/admin/accounts/{provider}")
async def save_account(provider: ProviderName, payload: CredentialRequest, _: str = Depends(require_admin)) -> dict[str, Any]:
    name = provider.value
    try:
        adapter = ProviderRegistry.create(name, payload.credential)
        account = await adapter.probe()
        vault.save(f"provider_{name}", payload.credential)
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
