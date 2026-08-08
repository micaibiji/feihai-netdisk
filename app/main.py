from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import (IntakeRequest, JobStatus, NotifyRequest, ProviderCredentialRequest,
                     SettingsRequest, StrmRequest, SubscriptionRequest, SubscriptionSourceRequest)
from .providers import PROVIDERS, ProviderRegistry
from .services import (create_media_bundle, generate_strm, media_relative_path, provider_auth_start,
                       search_resources, search_tmdb, send_notifications, trending_tmdb)
from .storage import JobStore
from .vault import CredentialVault

settings = get_settings()
store = JobStore(settings.database_path)
vault = CredentialVault(settings.data_dir)
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")


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


app = FastAPI(title=settings.app_name, version="0.3.0", lifespan=lifespan)
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


def require_login(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="账号或密码错误", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_login)):
    return templates.TemplateResponse(request=request, name="index.html", context={"app_name": settings.app_name})


@app.get("/api/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": "0.3.0", "database": settings.database_path.exists(), "strm_writable": os.access(settings.strm_dir, os.W_OK)}


@app.get("/api/overview")
async def overview(_: str = Depends(require_login)):
    ranking = await trending_tmdb(settings)
    subscriptions = store.list_subscriptions()
    accounts = provider_states()
    jobs = store.list(20)
    return {"ranking": ranking, "providers": accounts, "subscriptions": subscriptions, "jobs": jobs, "risk_events": store.list_risk_events(8), "settings": store.load_settings()}


def provider_states() -> list[dict]:
    records = {item["provider"]: item for item in store.provider_accounts()}
    output = []
    for provider in PROVIDERS:
        item = records[provider.name.value]
        environment_ready = bool(os.getenv(provider.credential_env, "").strip())
        connected = environment_ready or vault.configured(provider.name.value) or item["state"] == "connected"
        output.append({**item, "name": provider.name.value, "label": provider.label, "configured": connected, "state": "connected" if connected else item["state"]})
    return output


@app.get("/api/providers")
def providers(_: str = Depends(require_login)):
    return provider_states()


@app.post("/api/providers/{provider}/auth/start")
def start_provider_auth(provider: str, _: str = Depends(require_login)):
    if provider not in {item.name.value for item in PROVIDERS}:
        raise HTTPException(404, "未知网盘")
    result = provider_auth_start(settings, provider)
    store.update_provider(provider, state="authorizing", auth_method=result["mode"])
    return result


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
def intake(payload: IntakeRequest, _: str = Depends(require_login)):
    try:
        provider = ProviderRegistry.detect(str(payload.share_url))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    configured = next(item["configured"] for item in provider_states() if item["name"] == provider.name.value)
    job_status = JobStatus.QUEUED if configured else JobStatus.WAITING_AUTH
    return store.create(kind="share_intake", provider=provider.name.value, title=payload.title, status=job_status.value, detail={"share_url": str(payload.share_url), "target_folder": payload.target_folder, "auto_organize": payload.auto_organize, "message": "已进入独立网盘处理队列" if configured else f"请先授权 {provider.label}"})


@app.get("/api/tmdb/trending")
async def tmdb_trending(media_type: str = "all", _: str = Depends(require_login)):
    try:
        return await trending_tmdb(settings, media_type)
    except Exception as error:
        raise HTTPException(502, f"TMDB榜单查询失败：{error}") from error


@app.get("/api/tmdb/search")
async def tmdb_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        return await search_tmdb(settings, q)
    except Exception as error:
        raise HTTPException(502, f"TMDB查询失败：{error}") from error


@app.get("/api/search")
async def resource_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        resources, works = await asyncio.gather(search_resources(settings, q), search_tmdb(settings, q))
        return {"works": works, "resources": resources}
    except Exception as error:
        raise HTTPException(502, f"资源搜索失败：{error}") from error


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
    return store.load_settings()


@app.put("/api/settings")
def update_app_settings(payload: SettingsRequest, _: str = Depends(require_login)):
    store.save_settings(payload.model_dump())
    return {"saved": True, "settings": store.load_settings()}


@app.post("/api/notify")
async def notify(payload: NotifyRequest, _: str = Depends(require_login)):
    try:
        delivered = await send_notifications(settings, payload.message)
    except Exception as error:
        raise HTTPException(502, f"通知发送失败：{error}") from error
    return {"delivered": delivered, "configured": bool(delivered)}
