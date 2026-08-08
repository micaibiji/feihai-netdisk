from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import (DirectoryRequest, IntakeRequest, JobStatus, NotifyRequest,
                     ProviderCredentialRequest, ResourceValidationRequest,
                     SettingsRequest, StrmRequest, SubscriptionRequest, SubscriptionSourceRequest,
                     TmdbSettingsRequest)
from .provider_auth import (OpenListClient, ProviderAuthError, deserialize_secret,
                            poll_115_qr, serialize_secret, start_115_qr)
from .providers import PROVIDERS, ProviderRegistry
from .services import (create_media_bundle, generate_strm, media_relative_path, provider_auth_start,
                       search_resources, search_tmdb, send_notifications, trending_tmdb)
from .storage import JobStore
from .validation import validate_share_url
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


app = FastAPI(title=settings.app_name, version="0.4.2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


async def subscription_worker() -> None:
    await asyncio.sleep(15)
    while True:
        for subscription in store.list_subscriptions():
            if not subscription["enabled"]:
                continue
            try:
                results = await search_resources(settings, subscription["keyword"])
                new_count = 0
                for item in results[:30]:
                    record = store.upsert_resource(item)
                    validation = await validate_share_url(item["url"])
                    store.update_resource_validation(
                        record["fingerprint"], state=validation.state, reason=validation.reason,
                        checked_at=validation.checked_at, recheck_after=validation.recheck_after,
                    )
                    if validation.state != "valid":
                        continue
                    item["risk_status"] = "normal"
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
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not validate_session_token(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="index.html", context={"app_name": settings.app_name})


@app.get("/api/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": "0.4.2", "database": settings.database_path.exists(), "strm_writable": os.access(settings.strm_dir, os.W_OK)}


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
                        "openlist_configured": openlist_configured()})
    return {"ranking": ranking, "providers": accounts, "subscriptions": subscriptions, "jobs": jobs,
            "risk_events": store.list_risk_events(8), "settings": ui_settings}


def provider_states() -> list[dict]:
    records = {item["provider"]: item for item in store.provider_accounts()}
    output = []
    for provider in PROVIDERS:
        item = records[provider.name.value]
        environment_ready = bool(os.getenv(provider.credential_env, "").strip())
        connected = environment_ready or vault.configured(provider.name.value) or item["state"] == "connected"
        available_methods = list(provider.auth_methods)
        # Gateway driver setup is not presented as an in-page login until its QR/password
        # exchange can be completed and confirmed through this application.
        for unfinished in ("gateway_qr", "gateway_password"):
            if unfinished in available_methods:
                available_methods.remove(unfinished)
        if "oauth" in available_methods and not (settings.baidu_client_id and settings.baidu_redirect_uri):
            available_methods.remove("oauth")
        output.append({**item, "name": provider.name.value, "label": provider.label,
                       "configured": connected, "state": "connected" if connected else item["state"],
                       "auth_methods": available_methods,
                       "setup_required": not available_methods})
    return output


def openlist_configured() -> bool:
    return bool(settings.openlist_url and settings.admin_password)


def openlist_client() -> OpenListClient:
    return OpenListClient(
        settings.openlist_url,
        "admin",
        settings.admin_password,
    )


def tmdb_config() -> dict[str, str]:
    stored = store.load_settings()
    return {
        "api_key": vault.load_secret("tmdb_api_key") or settings.tmdb_api_key,
        "language": str(stored.get("tmdb_language") or "zh-CN"),
        "region": str(stored.get("tmdb_region") or "CN"),
    }


async def current_trending(media_type: str = "all", page: int = 1) -> dict:
    config = tmdb_config()
    return await trending_tmdb(settings, media_type, api_key=config["api_key"],
                               language=config["language"], region=config["region"],
                               page=page)


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
    return store.update_provider(provider, state="connected", account_mask=payload.account_mask, risk_status="normal", auth_method="local_encrypted")


@app.post("/api/providers/risk-scan")
async def risk_scan(_: str = Depends(require_login)):
    results = []
    gateway_ok = None
    if settings.openlist_url:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(f"{settings.openlist_url}/ping")
                gateway_ok = response.status_code < 500
        except Exception:
            gateway_ok = False
    for item in provider_states():
        if not item["configured"]:
            level, state, message, action = "info", "unknown", "尚未授权，未执行访问检测", "完成授权后再检测"
        elif item["auth_method"] == "gateway" and gateway_ok is False:
            level, state, message, action = "warning", "gateway_unreachable", "本机网盘网关无法访问", "暂停自动任务并保留原来源"
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
    validation = await validate_share_url(str(payload.share_url))
    if validation.state != "valid":
        raise HTTPException(409, f"入库前验证未通过：{validation.reason}")
    job = store.create(kind="share_intake", provider=provider.name.value, title=payload.title,
                       status=JobStatus.QUEUED.value,
                       detail={"share_url": str(payload.share_url), "target_folder": payload.target_folder,
                               "auto_organize": payload.auto_organize, "validation": validation.__dict__,
                               "message": "已通过最终验证，等待同网盘入库"})
    store.add_operation(action="queue_intake", target_type="job", target_id=str(job["id"]),
                        summary=f"{payload.title} → {provider.label}:{payload.target_folder}")
    return job


@app.get("/api/tmdb/trending")
async def tmdb_trending(media_type: str = "all", page: int = Query(default=1, ge=1, le=500),
                        _: str = Depends(require_login)):
    try:
        return await current_trending(media_type, page)
    except Exception as error:
        raise HTTPException(502, f"TMDB榜单查询失败：{error}") from error


@app.get("/api/tmdb/search")
async def tmdb_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        config = tmdb_config()
        return await search_tmdb(settings, q, api_key=config["api_key"],
                                 language=config["language"], region=config["region"])
    except Exception as error:
        raise HTTPException(502, f"TMDB查询失败：{error}") from error


@app.get("/api/search")
async def resource_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        config = tmdb_config()
        discovered, works = await asyncio.gather(
            search_resources(settings, q),
            search_tmdb(settings, q, api_key=config["api_key"], language=config["language"],
                        region=config["region"]),
        )
        semaphore = asyncio.Semaphore(4)

        async def check(item: dict) -> tuple[dict, str]:
            record = store.upsert_resource(item)
            if item["recognition_state"] != "recognized":
                return item, "pending_recognition"
            async with semaphore:
                validation = await validate_share_url(item["url"])
            store.update_resource_validation(
                record["fingerprint"], state=validation.state, reason=validation.reason,
                checked_at=validation.checked_at, recheck_after=validation.recheck_after,
            )
            item["validation_state"] = validation.state
            item["validation_reason"] = validation.reason
            return item, validation.state

        checked = await asyncio.gather(*(check(item) for item in discovered[:40]))
        visible = [item for item, state in checked if state == "valid"]
        counts: dict[str, int] = {"discovered": len(discovered), "valid": 0, "invalid": 0,
                                  "unverifiable": 0, "pending_recognition": 0}
        for _, validation_state in checked:
            counts[validation_state] = counts.get(validation_state, 0) + 1
        return {"works": works, "resources": visible, "progress": counts}
    except Exception as error:
        raise HTTPException(502, f"资源搜索失败：{error}") from error


@app.post("/api/resources/validate")
async def validate_resource(payload: ResourceValidationRequest, _: str = Depends(require_login)):
    try:
        result = await validate_share_url(str(payload.share_url))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
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
                   "openlist_configured": openlist_configured()})
    return values


@app.put("/api/settings")
def update_app_settings(payload: SettingsRequest, _: str = Depends(require_login)):
    store.save_settings(payload.model_dump())
    return {"saved": True, "settings": store.load_settings()}


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
    if not openlist_configured():
        raise HTTPException(503, "网盘连接服务尚未就绪，请稍后重试")
    mount_root = f"/{account['label']}"
    requested = payload.path or mount_root
    if requested == "/":
        requested = mount_root
    if requested != mount_root and not requested.startswith(mount_root + "/"):
        raise HTTPException(400, "目标目录必须位于当前网盘挂载点内")
    try:
        directories = await openlist_client().list_directories(requested)
    except (ProviderAuthError, httpx.HTTPError) as error:
        raise HTTPException(502, "读取网盘目录失败，内部连接服务暂时不可用") from error
    return {"provider": provider, "root": mount_root, "path": requested,
            "directories": directories}


@app.post("/api/notify")
async def notify(payload: NotifyRequest, _: str = Depends(require_login)):
    try:
        delivered = await send_notifications(settings, payload.message)
    except Exception as error:
        raise HTTPException(502, f"通知发送失败：{error}") from error
    return {"delivered": delivered, "configured": bool(delivered)}
