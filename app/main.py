from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import mimetypes
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import (CheckerSettingsRequest, DirectoryRequest, IntakeRequest, JobStatus, NotifyRequest,
                     PansouSettingsRequest,
                     ProviderCredentialRequest, ResourceValidationRequest,
                     PublicDirectoriesRequest, PublicDirectoryBrowseRequest,
                     SettingsRequest, StrmRequest, SubscriptionRequest, SubscriptionSourceRequest,
                     TmdbSettingsRequest)
from .provider_auth import (ProviderAuthError, deserialize_secret,
                            poll_115_qr, serialize_secret, start_115_qr)
from .providers import PROVIDERS, ProviderRegistry
from .services import (create_media_bundle, generate_strm, login_pansou, media_relative_path,
                       provider_auth_start, search_resources, search_tmdb, send_notifications,
                       test_pansou_connection, trending_tmdb)
from .storage import JobStore
from .validation import (ExternalValidatorError, should_show_resource,
                         test_checker_connection, validate_share_urls)
from .vault import CredentialVault

settings = get_settings()
store = JobStore(settings.database_path)
vault = CredentialVault(settings.data_dir)
templates = Jinja2Templates(directory="app/templates")
SESSION_COOKIE = "feihai_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.strm_dir.mkdir(parents=True, exist_ok=True)
    store.initialize()
    vault.initialize()
    worker = asyncio.create_task(subscription_worker())
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="0.4.7", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


async def subscription_worker() -> None:
    await asyncio.sleep(15)
    while True:
        for subscription in store.list_subscriptions():
            if not subscription["enabled"]:
                continue
            try:
                results = await search_from_pansou(subscription["keyword"])
                checks = await check_external_links([item["url"] for item in results[:30]])
                new_count = 0
                for item in results[:30]:
                    record = store.upsert_resource(item)
                    validation = checks[item["url"]]
                    store.update_resource_validation(
                        record["fingerprint"], state=validation.state, reason=validation.reason,
                        checked_at=validation.checked_at, recheck_after=validation.recheck_after,
                    )
                    if not should_show_resource(validation.state):
                        continue
                    item["risk_status"] = "normal" if validation.state == "valid" else "unknown"
                    selected = store.add_subscription_source(subscription["id"], item, settings.provider_priority)
                    if store.mark_seen(subscription["id"], item["fingerprint"]):
                        new_count += 1
                        store.create(kind="subscription_match", provider=item["provider"], title=subscription["keyword"], status=JobStatus.QUEUED.value, detail={"share_url": item["url"], "selected": selected["share_url"] == item["url"], "message": f"发现 {item['provider_label']} 更新至 S{item['season']:02d}E{item['episode']:02d}"})
                if new_count:
                    await send_notifications(settings, f"飞海网盘：{subscription['keyword']} 发现 {new_count} 个新来源，已自动选择最新来源。")
            except Exception as error:
                store.add_risk_event("system", "warning", "subscription_check", f"{subscription['keyword']} 检查失败：{str(error)[:160]}", "保留原来源，等待下次重试")
            finally:
                store.mark_checked(subscription["id"])
        await asyncio.sleep(settings.subscription_interval_seconds)


def create_session_token(username: str, now: int | None = None) -> str:
    expires_at = (now or int(time.time())) + SESSION_MAX_AGE
    payload = f"{username}:{expires_at}"
    key = hashlib.sha256(settings.admin_password.encode("utf-8")).digest()
    signature = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii").rstrip("=")


def validate_session_token(token: str | None, now: int | None = None) -> str | None:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        username, expires_at_raw, signature = decoded.rsplit(":", 2)
        payload = f"{username}:{expires_at_raw}"
        key = hashlib.sha256(settings.admin_password.encode("utf-8")).digest()
        expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at_raw) < (now or int(time.time())):
            return None
        if not secrets.compare_digest(username, settings.admin_username):
            return None
        return username
    except (ValueError, UnicodeDecodeError):
        return None


def require_login(request: Request) -> str:
    username = validate_session_token(request.cookies.get(SESSION_COOKIE))
    if not username:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return username


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if validate_session_token(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"app_name": settings.app_name, "error": ""})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    valid_user = secrets.compare_digest(username, settings.admin_username)
    valid_password = secrets.compare_digest(password, settings.admin_password)
    if not (valid_user and valid_password):
        return templates.TemplateResponse(request=request, name="login.html", context={"app_name": settings.app_name, "error": "账号或密码不正确"}, status_code=401)
    response = RedirectResponse("/?verified=1", status_code=303)
    response.set_cookie(SESSION_COOKIE, create_session_token(username), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=False, path="/")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    is_admin = bool(validate_session_token(request.cookies.get(SESSION_COOKIE)))
    return templates.TemplateResponse(request=request, name="index.html",
                                      context={"app_name": settings.app_name, "is_admin": is_admin})


@app.get("/api/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": "0.4.7", "database": settings.database_path.exists(), "strm_writable": os.access(settings.strm_dir, os.W_OK)}


@app.post("/api/verify-password")
async def verify_password(request: Request, _: str = Depends(require_login)):
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="请输入验证密码") from error
    password = str(payload.get("password", ""))
    if not secrets.compare_digest(password, settings.admin_password):
        raise HTTPException(status_code=401, detail="验证密码不正确")
    return {"verified": True}


@app.get("/api/overview")
async def overview(_: str = Depends(require_login)):
    ranking = await current_trending()
    subscriptions = store.list_subscriptions()
    accounts = provider_states()
    jobs = store.list(20)
    ui_settings = store.load_settings()
    current_tmdb = tmdb_config()
    ui_settings.update({"tmdb_configured": bool(current_tmdb["api_key"]),
                        "tmdb_language": current_tmdb["language"],
                        "tmdb_region": current_tmdb["region"],
                        "native_mounts": native_mount_status(),
                        **integration_public_settings()})
    return {"ranking": ranking, "providers": accounts, "subscriptions": subscriptions, "jobs": jobs,
            "risk_events": store.list_risk_events(8), "settings": ui_settings,
            "public_directories": public_directory_entries()}


@app.get("/api/public/session")
def public_session(request: Request):
    username = validate_session_token(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": bool(username), "username": username or ""}


@app.get("/api/public/overview")
async def public_overview():
    return {"ranking": await current_trending(), "public_directories": public_directory_entries()}


def provider_states() -> list[dict]:
    records = {item["provider"]: item for item in store.provider_accounts()}
    output = []
    for provider in PROVIDERS:
        item = records[provider.name.value]
        environment_ready = bool(os.getenv(provider.credential_env, "").strip())
        native_ready = native_mount_available(provider.name.value)
        connected = (native_ready or environment_ready or vault.configured(provider.name.value)
                     or item["state"] == "connected")
        available_methods = list(provider.auth_methods)
        # Gateway driver setup is not presented as an in-page login until its QR/password
        # exchange can be completed and confirmed through this application.
        for unfinished in ("gateway_qr", "gateway_password"):
            if unfinished in available_methods:
                available_methods.remove(unfinished)
        if "oauth" in available_methods and not (settings.baidu_client_id and settings.baidu_redirect_uri):
            available_methods.remove("oauth")
        auth_method = "fnos_mount" if native_ready else item["auth_method"]
        account_mask = "飞牛原生挂载" if native_ready else item["account_mask"]
        output.append({**item, "name": provider.name.value, "label": provider.label,
                       "configured": connected, "state": "connected" if connected else item["state"],
                       "auth_method": auth_method, "account_mask": account_mask,
                       "native_mount": native_ready,
                       "auth_methods": available_methods,
                       "setup_required": not available_methods})
    return output


def native_mount_available(provider: str) -> bool:
    if not settings.native_mount_enabled(provider):
        return False
    root = settings.native_mount_path(provider)
    try:
        return root.is_dir() and os.access(root, os.R_OK | os.X_OK)
    except OSError:
        return False


def native_mount_status() -> dict[str, bool]:
    return {provider.name.value: native_mount_available(provider.name.value)
            for provider in PROVIDERS}


def list_native_directories(provider: str, label: str, requested: str) -> tuple[str, str, list[dict]]:
    virtual_root = f"/{label}"
    virtual_path = requested or virtual_root
    if virtual_path == "/":
        virtual_path = virtual_root
    if virtual_path != virtual_root and not virtual_path.startswith(virtual_root + "/"):
        raise HTTPException(400, "目标目录必须位于当前网盘挂载点内")

    suffix = virtual_path[len(virtual_root):].lstrip("/")
    relative = PurePosixPath(suffix) if suffix else PurePosixPath()
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(400, "目标目录格式不正确")

    root = settings.native_mount_path(provider).resolve()
    target = (root / Path(*relative.parts)).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "目标目录超出当前网盘挂载点")
    try:
        entries = sorted(
            (entry for entry in os.scandir(target) if entry.is_dir(follow_symlinks=False)),
            key=lambda entry: entry.name.casefold(),
        )
    except FileNotFoundError as error:
        raise HTTPException(404, "目标目录不存在或网盘已经断开") from error
    except (PermissionError, OSError) as error:
        raise HTTPException(502, "飞牛远程挂载暂时无法读取，请检查网盘连接状态") from error
    directories = [
        {"name": entry.name,
         "path": f"{virtual_path.rstrip('/')}/{entry.name}",
         "modified": ""}
        for entry in entries
    ]
    return virtual_root, virtual_path, directories


def provider_label(provider: str) -> str:
    match = next((item.label for item in PROVIDERS if item.name.value == provider), None)
    if not match:
        raise HTTPException(404, "未知网盘")
    return match


def public_directory_id(provider: str, path: str) -> str:
    return hashlib.sha256(f"{provider}:{path}".encode("utf-8")).hexdigest()[:16]


def public_directory_entries() -> list[dict]:
    raw = store.load_settings().get("public_directories", [])
    if not isinstance(raw, list):
        return []
    output = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", ""))
        path = str(item.get("path", ""))
        if provider not in {entry.name.value for entry in PROVIDERS} or not path:
            continue
        output.append({
            "id": public_directory_id(provider, path),
            "provider": provider,
            "provider_label": provider_label(provider),
            "path": path,
            "label": str(item.get("label") or PurePosixPath(path).name or provider_label(provider)),
        })
    return output


def list_public_directory_contents(entry: dict, relative_path: str) -> dict:
    provider = entry["provider"]
    label = provider_label(provider)
    base_virtual = entry["path"]
    relative = PurePosixPath(relative_path) if relative_path else PurePosixPath()
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(400, "目录格式不正确")
    requested = base_virtual.rstrip("/")
    if relative.parts:
        requested += "/" + "/".join(relative.parts)

    virtual_root = f"/{label}"
    if base_virtual != virtual_root and not base_virtual.startswith(virtual_root + "/"):
        raise HTTPException(400, "公开目录配置无效")
    suffix = requested[len(virtual_root):].lstrip("/")
    mount_root = settings.native_mount_path(provider).resolve()
    target = (mount_root / Path(*PurePosixPath(suffix).parts)).resolve()
    base_suffix = base_virtual[len(virtual_root):].lstrip("/")
    base_target = (mount_root / Path(*PurePosixPath(base_suffix).parts)).resolve()
    if target != base_target and base_target not in target.parents:
        raise HTTPException(400, "不能访问公开目录之外的内容")
    if target != mount_root and mount_root not in target.parents:
        raise HTTPException(400, "目录超出网盘挂载范围")
    try:
        scanned = [item for item in os.scandir(target) if not item.is_symlink()]
        scanned.sort(key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.casefold()))
        contents = []
        for item in scanned:
            is_directory = item.is_dir(follow_symlinks=False)
            stat = item.stat(follow_symlinks=False)
            item_relative = "/".join((*relative.parts, item.name))
            contents.append({
                "name": item.name,
                "type": "directory" if is_directory else "file",
                "path": item_relative,
                "size": 0 if is_directory else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            })
    except FileNotFoundError as error:
        raise HTTPException(404, "公开目录不存在或网盘已经断开") from error
    except (PermissionError, OSError) as error:
        raise HTTPException(502, "飞牛远程挂载暂时无法读取") from error
    return {
        "id": entry["id"], "label": entry["label"], "provider": provider,
        "provider_label": entry["provider_label"], "path": "/".join(relative.parts),
        "contents": contents,
    }


def resolve_public_file(entry: dict, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if not relative.parts or relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(400, "文件路径格式不正确")
    provider = entry["provider"]
    label = provider_label(provider)
    virtual_root = f"/{label}"
    base_virtual = entry["path"]
    base_suffix = base_virtual[len(virtual_root):].lstrip("/")
    mount_root = settings.native_mount_path(provider).resolve()
    base_target = (mount_root / Path(*PurePosixPath(base_suffix).parts)).resolve()
    candidate = base_target / Path(*relative.parts)
    target = candidate.resolve()
    if target != base_target and base_target not in target.parents:
        raise HTTPException(400, "不能访问公开目录之外的文件")
    if candidate.is_symlink() or not target.is_file():
        raise HTTPException(404, "视频文件不存在")
    return target


def tmdb_config() -> dict[str, str]:
    stored = store.load_settings()
    return {
        "api_key": vault.load_secret("tmdb_api_key") or settings.tmdb_api_key,
        "language": str(stored.get("tmdb_language") or "zh-CN"),
        "region": str(stored.get("tmdb_region") or "CN"),
    }


def pansou_config() -> dict[str, str | bool]:
    stored = store.load_settings()
    # External integrations are configured explicitly from the web UI.
    base_url = str(stored.get("pansou_base_url") or "").rstrip("/")
    username = vault.load_secret("pansou_username")
    password = vault.load_secret("pansou_password")
    token = vault.load_secret("pansou_token")
    return {
        "base_url": base_url,
        "api_path": str(stored.get("pansou_api_path") or "/api/search"),
        "source": str(stored.get("pansou_source") or "all"),
        "username": username,
        "password": password,
        "token": token,
        "configured": bool(base_url),
        "auth_configured": bool(token or (username and password)),
    }


def checker_config() -> dict[str, str | int | bool]:
    stored = store.load_settings()
    base_url = str(stored.get("checker_base_url") or "").rstrip("/")
    token = vault.load_secret("checker_token")
    return {
        "base_url": base_url,
        "api_path": str(stored.get("checker_api_path") or "/api/v1/links/check"),
        "token": token,
        "timeout_seconds": int(stored.get("checker_timeout_seconds") or 35),
        "cache_minutes": int(stored.get("checker_cache_minutes") or 120),
        "configured": bool(base_url),
        "auth_configured": bool(token),
    }


def integration_public_settings() -> dict[str, object]:
    pansou = pansou_config()
    checker = checker_config()
    return {
        "pansou_base_url": pansou["base_url"],
        "pansou_api_path": pansou["api_path"],
        "pansou_source": pansou["source"],
        "pansou_configured": pansou["configured"],
        "pansou_auth_configured": pansou["auth_configured"],
        "checker_base_url": checker["base_url"],
        "checker_api_path": checker["api_path"],
        "checker_timeout_seconds": checker["timeout_seconds"],
        "checker_cache_minutes": checker["cache_minutes"],
        "checker_configured": checker["configured"],
        "checker_auth_configured": checker["auth_configured"],
    }


async def search_from_pansou(query: str) -> list[dict]:
    config = pansou_config()
    try:
        return await search_resources(
            settings, query, base_url=str(config["base_url"]), api_path=str(config["api_path"]),
            source=str(config["source"]), token=str(config["token"]),
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 401 or not (config["username"] and config["password"]):
            raise
        token = await login_pansou(str(config["base_url"]), str(config["username"]), str(config["password"]))
        vault.save_secret("pansou_token", token)
        return await search_resources(
            settings, query, base_url=str(config["base_url"]), api_path=str(config["api_path"]),
            source=str(config["source"]), token=token,
        )


async def check_external_links(urls: list[str]):
    config = checker_config()
    return await validate_share_urls(
        urls, base_url=str(config["base_url"]), api_path=str(config["api_path"]),
        token=str(config["token"]), timeout_seconds=int(config["timeout_seconds"]),
        cache_minutes=int(config["cache_minutes"]),
    )


async def current_trending(media_type: str = "all", page: int = 1, year: int | None = None,
                           genre: str | None = None, country: str | None = None) -> dict:
    config = tmdb_config()
    return await trending_tmdb(settings, media_type, api_key=config["api_key"],
                               language=config["language"], region=config["region"],
                               page=page, year=year, genre=genre, country=country)


@app.get("/api/providers")
def providers(_: str = Depends(require_login)):
    return provider_states()


@app.post("/api/providers/{provider}/auth/start")
async def start_provider_auth(provider: str, _: str = Depends(require_login)):
    if provider not in {item.name.value for item in PROVIDERS}:
        raise HTTPException(404, "未知网盘")
    session_id = uuid.uuid4().hex
    try:
        if provider == "115":
            public, secret_payload = await start_115_qr()
            secret_key = f"auth_{session_id}"
            vault.save_secret(secret_key, serialize_secret(secret_payload))
            result = store.create_auth_session(
                session_id=session_id, provider=provider, method="qr", state="waiting",
                public_payload=public, secret_key=secret_key,
                expires_at=(datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
            )
            store.update_provider(provider, state="authorizing", auth_method="qr")
            return result
        result = provider_auth_start(settings, provider)
        if not result["ready"]:
            raise HTTPException(409, result["message"])
        session = store.create_auth_session(
            session_id=session_id, provider=provider, method=result["mode"], state="waiting",
            public_payload={"authorize_url": result["url"], "message": result["message"]},
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        )
        store.update_provider(provider, state="authorizing", auth_method=result["mode"])
        return session
    except ProviderAuthError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/providers/auth/{session_id}")
async def provider_auth_status(session_id: str, _: str = Depends(require_login)):
    try:
        session = store.get_auth_session(session_id, include_secret=True)
    except KeyError as error:
        raise HTTPException(404, "授权会话不存在") from error
    if session["state"] in {"succeeded", "expired", "canceled", "failed"}:
        session.pop("secret_key", None)
        return session
    if session["provider"] != "115" or session["method"] != "qr":
        session.pop("secret_key", None)
        return session
    if session.get("expires_at") and datetime.fromisoformat(session["expires_at"]) < datetime.now(UTC):
        return store.update_auth_session(session_id, state="expired", error="二维码已过期")
    try:
        secret = deserialize_secret(vault.load_secret(session["secret_key"]))
        auth_state, message, credential = await poll_115_qr(secret)
        public = {**session["public_payload"], "message": message}
        output = store.update_auth_session(session_id, state=auth_state, public_payload=public)
        if credential:
            vault.save("115", credential)
            vault.delete_secret(session["secret_key"])
            store.update_provider("115", state="connected", account_mask="115 已授权账号",
                                  risk_status="normal", auth_method="qr")
        return output
    except (ProviderAuthError, httpx.HTTPError) as error:
        message = str(error).strip() or "115 状态服务暂时没有响应，请刷新二维码重试"
        raise HTTPException(502, f"查询扫码状态失败：{message}") from error


@app.post("/api/providers/{provider}/credential")
def save_provider_credential(provider: str, payload: ProviderCredentialRequest, _: str = Depends(require_login)):
    if provider not in {item.name.value for item in PROVIDERS}:
        raise HTTPException(404, "未知网盘")
    vault.save(provider, payload.credential)
    return store.update_provider(provider, state="connected", account_mask=payload.account_mask,
                                 risk_status="unknown", auth_method="token")


@app.post("/api/providers/risk-scan")
async def risk_scan(_: str = Depends(require_login)):
    results = []
    for item in provider_states():
        if not item["configured"]:
            level, state, message, action = "info", "unknown", "尚未授权，未执行访问检测", "完成授权后再检测"
        elif item.get("native_mount") and not native_mount_available(item["name"]):
            level, state, message, action = "warning", "mount_unavailable", "飞牛远程挂载无法访问", "请在文件管理中重新连接网盘"
        elif item.get("native_mount"):
            level, state, message, action = "safe", "normal", "飞牛远程挂载可读，连接正常", "保持低频访问"
        else:
            level, state, message, action = "safe", "normal", "凭证存在，未发现本地异常", "保持低频访问"
        store.update_provider(item["name"], risk_status=state)
        results.append(store.add_risk_event(item["name"], level, "credential_probe", message, action))
    return results


@app.get("/api/risk-events")
def risk_events(_: str = Depends(require_login)):
    return store.list_risk_events()


@app.get("/api/jobs")
def jobs(limit: int = Query(default=50, ge=1, le=200), _: str = Depends(require_login)):
    return store.list(limit)


@app.post("/api/intake", status_code=status.HTTP_201_CREATED)
async def intake(payload: IntakeRequest, _: str = Depends(require_login)):
    try:
        provider = ProviderRegistry.detect(str(payload.share_url))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    configured = next(item["configured"] for item in provider_states() if item["name"] == provider.name.value)
    if not configured:
        return store.create(kind="share_intake", provider=provider.name.value, title=payload.title,
                            status=JobStatus.WAITING_AUTH.value,
                            detail={"share_url": str(payload.share_url), "target_folder": payload.target_folder,
                                    "auto_organize": payload.auto_organize, "message": f"请先授权 {provider.label}"})
    try:
        validation = (await check_external_links([str(payload.share_url)]))[str(payload.share_url)]
    except ExternalValidatorError:
        validation = None
    if validation is not None and not should_show_resource(validation.state):
        raise HTTPException(409, f"入库前检测为失效：{validation.reason}")
    validation_detail = validation.__dict__ if validation is not None else {
        "state": "detector_unavailable", "reason": "检测网站暂时不可用",
    }
    validation_message = (
        "检测网站确认有效，等待同网盘入库"
        if validation is not None and validation.state == "valid"
        else "未被检测网站判定为失效，已进入入库队列"
    )
    job = store.create(kind="share_intake", provider=provider.name.value, title=payload.title,
                       status=JobStatus.QUEUED.value,
                       detail={"share_url": str(payload.share_url), "target_folder": payload.target_folder,
                               "auto_organize": payload.auto_organize, "validation": validation_detail,
                               "message": validation_message})
    store.add_operation(action="queue_intake", target_type="job", target_id=str(job["id"]),
                        summary=f"{payload.title} → {provider.label}:{payload.target_folder}")
    return job


@app.get("/api/tmdb/trending")
async def tmdb_trending(media_type: str = "all", page: int = Query(default=1, ge=1, le=500),
                        year: int | None = Query(default=None, ge=1900, le=2200),
                        genre: str | None = Query(default=None, pattern="^(action|animation|comedy|crime|documentary|drama|family|mystery|romance|scifi)$"),
                        country: str | None = Query(default=None, pattern="^(CN|US|GB|JP|KR|HK|TW|IN|FR|DE)$")):
    try:
        return await current_trending(media_type, page, year, genre, country)
    except Exception as error:
        raise HTTPException(502, f"TMDB榜单查询失败：{error}") from error


@app.get("/api/tmdb/search")
async def tmdb_search(q: str = Query(min_length=1, max_length=100)):
    try:
        config = tmdb_config()
        return await search_tmdb(settings, q, api_key=config["api_key"],
                                 language=config["language"], region=config["region"])
    except Exception as error:
        raise HTTPException(502, f"TMDB查询失败：{error}") from error


def resource_for_viewer(item: dict, is_admin: bool) -> dict:
    """Do not send transferable share links to unauthenticated visitors."""
    result = item.copy()
    if not is_admin:
        result.pop("url", None)
        result.pop("validation_reason", None)
    return result


@app.get("/api/search")
async def resource_search(request: Request, q: str = Query(min_length=1, max_length=100)):
    try:
        config = tmdb_config()
        discovered, works = await asyncio.gather(
            search_from_pansou(q),
            search_tmdb(settings, q, api_key=config["api_key"], language=config["language"],
                        region=config["region"]),
        )
        records = {item["url"]: store.upsert_resource(item) for item in discovered[:100]}
        try:
            validations = await check_external_links(list(records))
            detector = {"status": "connected", "message": "你的检测网站已完成检查"}
        except ExternalValidatorError as error:
            validations = {}
            detector = {"status": "unavailable", "message": str(error)}

        async def check(item: dict) -> tuple[dict, str]:
            record = records[item["url"]]
            validation = validations.get(item["url"])
            if validation is None:
                item["validation_state"] = "detector_unavailable"
                item["validation_reason"] = "检测网站暂时不可用"
                return item, "detector_unavailable"
            store.update_resource_validation(
                record["fingerprint"], state=validation.state, reason=validation.reason,
                checked_at=validation.checked_at, recheck_after=validation.recheck_after,
            )
            item["validation_state"] = validation.state
            item["validation_reason"] = validation.reason
            return item, validation.state

        checked = await asyncio.gather(*(check(item) for item in discovered[:100]))
        is_admin = bool(validate_session_token(request.cookies.get(SESSION_COOKIE)))
        visible = [resource_for_viewer(item, is_admin) for item, state in checked
                   if should_show_resource(state)]
        counts: dict[str, int] = {"discovered": len(discovered), "valid": 0, "invalid": 0,
                                  "unverifiable": 0, "pending_recognition": 0,
                                  "detector_unavailable": 0}
        for _, validation_state in checked:
            counts[validation_state] = counts.get(validation_state, 0) + 1
        return {"works": works, "resources": visible, "progress": counts,
                "detector": detector}
    except Exception as error:
        raise HTTPException(502, f"资源搜索失败：{error}") from error


@app.post("/api/resources/validate")
async def validate_resource(payload: ResourceValidationRequest, _: str = Depends(require_login)):
    try:
        result = (await check_external_links([str(payload.share_url)]))[str(payload.share_url)]
    except (ValueError, ExternalValidatorError) as error:
        raise HTTPException(503, str(error)) from error
    return result.__dict__


@app.get("/api/subscriptions")
def subscriptions(_: str = Depends(require_login)):
    return store.list_subscriptions()


@app.post("/api/subscriptions", status_code=201)
def create_subscription(payload: SubscriptionRequest, _: str = Depends(require_login)):
    return store.create_subscription(payload.keyword, payload.auto_intake, payload.media_type, payload.year)


@app.post("/api/subscriptions/{subscription_id}/sources", status_code=201)
def add_subscription_source(subscription_id: int, payload: SubscriptionSourceRequest, _: str = Depends(require_login)):
    try:
        provider = ProviderRegistry.detect(str(payload.share_url))
        item = {"provider": provider.name.value, "url": str(payload.share_url), "title": payload.title, "season": payload.season, "episode": payload.episode, "quality": payload.quality, "source": payload.source, "risk_status": "unknown"}
        return store.add_subscription_source(subscription_id, item, settings.provider_priority)
    except (ValueError, KeyError) as error:
        raise HTTPException(400, str(error)) from error


@app.patch("/api/subscriptions/{subscription_id}")
def update_subscription(subscription_id: int, enabled: bool, _: str = Depends(require_login)):
    store.set_subscription_enabled(subscription_id, enabled)
    return {"updated": True}


@app.post("/api/organize", status_code=201)
def organize(title: str, media_type: str, play_url: str, year: int | None = None, season: int = 1, episode: int = 0, _: str = Depends(require_login)):
    files = create_media_bundle(settings, title=title, media_type=media_type, play_url=play_url, year=year, season=season, episode=episode)
    job = store.create(kind="organize", provider="selected", title=title, status=JobStatus.COMPLETED.value, detail={"files": files, "message": "已按统一命名生成 STRM 与 NFO，等待飞牛影视扫描"})
    return {"files": files, "job": job}


@app.get("/api/naming-preview")
def naming_preview(title: str, media_type: str = "tv", year: int | None = None, season: int = 1, episode: int = 1, _: str = Depends(require_login)):
    return {"path": media_relative_path(title, media_type, year, season, episode)}


@app.post("/api/strm", status_code=201)
def create_strm(payload: StrmRequest, _: str = Depends(require_login)):
    target = generate_strm(settings, payload.relative_dir, payload.name, str(payload.play_url))
    return {"created": True, "path": str(target.relative_to(settings.strm_dir))}


@app.get("/api/settings")
def get_app_settings(_: str = Depends(require_login)):
    values = store.load_settings()
    tmdb = tmdb_config()
    values.update({"tmdb_configured": bool(tmdb["api_key"]), "tmdb_language": tmdb["language"],
                   "tmdb_region": tmdb["region"],
                   "native_mounts": native_mount_status(),
                   **integration_public_settings()})
    return values


@app.put("/api/settings")
def update_app_settings(payload: SettingsRequest, _: str = Depends(require_login)):
    store.save_settings(payload.model_dump())
    return {"saved": True, "settings": store.load_settings()}


@app.get("/api/settings/public-directories")
def get_public_directories(_: str = Depends(require_login)):
    return {"entries": public_directory_entries()}


@app.put("/api/settings/public-directories")
def update_public_directories(payload: PublicDirectoriesRequest, _: str = Depends(require_login)):
    normalized = []
    seen = set()
    for item in payload.entries:
        provider = item.provider.value
        if not native_mount_available(provider):
            raise HTTPException(409, f"{provider_label(provider)}尚未在飞牛文件管理中挂载")
        _, path, _ = list_native_directories(provider, provider_label(provider), item.path)
        key = (provider, path)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"provider": provider, "path": path,
                           "label": item.label.strip() or PurePosixPath(path).name})
    store.save_settings({"public_directories": normalized})
    return {"saved": True, "entries": public_directory_entries()}


@app.post("/api/public/directories/{directory_id}/browse")
def browse_public_directory(directory_id: str, payload: PublicDirectoryBrowseRequest):
    entry = next((item for item in public_directory_entries() if item["id"] == directory_id), None)
    if not entry:
        raise HTTPException(404, "公开目录不存在或已经取消公开")
    if not native_mount_available(entry["provider"]):
        raise HTTPException(503, "网盘暂时未连接")
    return list_public_directory_contents(entry, payload.path)


@app.get("/api/public/directories/{directory_id}/stream")
def stream_public_video(directory_id: str, path: str = Query(min_length=1, max_length=1000)):
    entry = next((item for item in public_directory_entries() if item["id"] == directory_id), None)
    if not entry:
        raise HTTPException(404, "公开目录不存在或已经取消公开")
    if not native_mount_available(entry["provider"]):
        raise HTTPException(503, "网盘暂时未连接")
    target = resolve_public_file(entry, path)
    allowed = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".ts", ".m2ts"}
    if target.suffix.lower() not in allowed:
        raise HTTPException(415, "该文件不支持在线播放")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name,
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff"})


@app.get("/api/settings/integrations")
def get_integration_settings(_: str = Depends(require_login)):
    return integration_public_settings()


async def _resolve_pansou_token(base_url: str, username: str, password: str, token: str) -> str:
    if token:
        return token
    if username and password:
        return await login_pansou(base_url, username, password)
    return ""


@app.put("/api/settings/pansou")
async def update_pansou_settings(payload: PansouSettingsRequest, _: str = Depends(require_login)):
    current = pansou_config()
    if payload.clear_credentials:
        for key in ("pansou_username", "pansou_password", "pansou_token"):
            vault.delete_secret(key)
        current.update({"username": "", "password": "", "token": ""})
    username = payload.username or str(current["username"])
    password = payload.password or str(current["password"])
    token = payload.token or str(current["token"])
    try:
        token = await _resolve_pansou_token(payload.base_url.rstrip("/"), username, password, token)
        test = await test_pansou_connection(payload.base_url, token)
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(400, f"Pansou 连接测试失败：{str(error)}") from error
    if payload.username:
        vault.save_secret("pansou_username", payload.username)
    if payload.password:
        vault.save_secret("pansou_password", payload.password)
    if token:
        vault.save_secret("pansou_token", token)
    store.save_settings({
        "pansou_base_url": payload.base_url.rstrip("/"),
        "pansou_api_path": payload.api_path,
        "pansou_source": payload.source,
    })
    return {"saved": True, "test": test, "settings": integration_public_settings()}


@app.post("/api/settings/pansou/test")
async def test_pansou(_: str = Depends(require_login)):
    config = pansou_config()
    if not config["configured"]:
        raise HTTPException(409, "尚未配置 Pansou 地址")
    try:
        token = await _resolve_pansou_token(
            str(config["base_url"]), str(config["username"]),
            str(config["password"]), str(config["token"]),
        )
        if token and token != config["token"]:
            vault.save_secret("pansou_token", token)
        return await test_pansou_connection(str(config["base_url"]), token)
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(502, f"Pansou 连接失败：{str(error)}") from error


@app.put("/api/settings/checker")
async def update_checker_settings(payload: CheckerSettingsRequest, _: str = Depends(require_login)):
    if payload.clear_token:
        vault.delete_secret("checker_token")
    token = payload.token or ("" if payload.clear_token else vault.load_secret("checker_token"))
    try:
        test = await test_checker_connection(payload.base_url, token)
    except ExternalValidatorError as error:
        raise HTTPException(400, f"检测网站连接测试失败：{str(error)}") from error
    if payload.token:
        vault.save_secret("checker_token", payload.token)
    store.save_settings({
        "checker_base_url": payload.base_url.rstrip("/"),
        "checker_api_path": payload.api_path,
        "checker_timeout_seconds": payload.timeout_seconds,
        "checker_cache_minutes": payload.cache_minutes,
    })
    return {"saved": True, "test": test, "settings": integration_public_settings()}


@app.post("/api/settings/checker/test")
async def test_checker(_: str = Depends(require_login)):
    config = checker_config()
    if not config["configured"]:
        raise HTTPException(409, "尚未配置检测网站地址")
    try:
        return await test_checker_connection(str(config["base_url"]), str(config["token"]))
    except ExternalValidatorError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/settings/tmdb")
def get_tmdb_settings(_: str = Depends(require_login)):
    config = tmdb_config()
    return {"configured": bool(config["api_key"]), "api_key_mask": "••••••••" if config["api_key"] else "",
            "language": config["language"], "region": config["region"]}


@app.put("/api/settings/tmdb")
async def update_tmdb_settings(payload: TmdbSettingsRequest, _: str = Depends(require_login)):
    if payload.api_key:
        try:
            await trending_tmdb(settings, "movie", api_key=payload.api_key, language=payload.language,
                                region=payload.region)
        except httpx.HTTPError as error:
            raise HTTPException(400, "TMDB 密钥测试失败，请检查密钥和网络") from error
        vault.save_secret("tmdb_api_key", payload.api_key)
    elif not vault.load_secret("tmdb_api_key") and not settings.tmdb_api_key:
        raise HTTPException(400, "请输入 TMDB API 密钥")
    store.save_settings({"tmdb_language": payload.language, "tmdb_region": payload.region})
    return {"saved": True, "test": "connected", "settings": get_tmdb_settings(_)}


@app.post("/api/settings/tmdb/test")
async def test_tmdb(_: str = Depends(require_login)):
    try:
        ranking = await current_trending("movie")
    except httpx.HTTPError as error:
        raise HTTPException(502, "TMDB 连接测试失败") from error
    if not ranking["live"]:
        raise HTTPException(409, "尚未配置 TMDB API 密钥")
    return {"connected": True, "updated_at": ranking["updated_at"], "items": len(ranking["items"])}


@app.post("/api/providers/{provider}/directories")
async def provider_directories(provider: str, payload: DirectoryRequest, _: str = Depends(require_login)):
    if provider not in {item.name.value for item in PROVIDERS}:
        raise HTTPException(404, "未知网盘")
    account = next(item for item in provider_states() if item["name"] == provider)
    if not account["configured"]:
        raise HTTPException(409, f"请先授权 {account['label']}")
    if not native_mount_available(provider):
        raise HTTPException(503, "飞牛远程挂载尚未接入，请先在飞牛文件管理中挂载网盘")
    mount_root, requested, directories = list_native_directories(
        provider, account["label"], payload.path or ""
    )
    return {"provider": provider, "root": mount_root, "path": requested,
            "directories": directories, "connection": "fnos_mount"}


@app.post("/api/notify")
async def notify(payload: NotifyRequest, _: str = Depends(require_login)):
    try:
        delivered = await send_notifications(settings, payload.message)
    except Exception as error:
        raise HTTPException(502, f"通知发送失败：{error}") from error
    return {"delivered": delivered, "configured": bool(delivered)}
