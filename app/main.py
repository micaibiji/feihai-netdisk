from __future__ import annotations

import secrets
import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import get_settings
from .models import IntakeRequest, JobStatus, NotifyRequest, StrmRequest, SubscriptionRequest
from .providers import ProviderRegistry
from .services import generate_strm, search_resources, search_tmdb, send_notifications
from .storage import JobStore

settings = get_settings()
store = JobStore(settings.database_path)
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.strm_dir.mkdir(parents=True, exist_ok=True)
    store.initialize()
    worker = asyncio.create_task(subscription_worker())
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


async def subscription_worker() -> None:
    while True:
        for subscription in store.list_subscriptions():
            if not subscription["enabled"]:
                continue
            try:
                results = await search_resources(settings, subscription["keyword"])
                new_items = [
                    item
                    for item in results
                    if store.mark_seen(subscription["id"], item["fingerprint"])
                ]
                for item in new_items[:10]:
                    store.create(
                        kind="subscription_match",
                        provider=item["provider"],
                        title=subscription["keyword"],
                        status=JobStatus.QUEUED.value,
                        detail={
                            "share_url": item["url"],
                            "message": f"追更发现新资源：{item['provider_label']}",
                        },
                    )
                if new_items:
                    await send_notifications(
                        settings,
                        f"飞海网盘：{subscription['keyword']} 发现 {len(new_items)} 个新资源。",
                    )
            except Exception:
                pass
            finally:
                store.mark_checked(subscription["id"])
        await asyncio.sleep(settings.subscription_interval_seconds)


def require_login(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_login)):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": settings.app_name},
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": "0.1.0"}


@app.get("/api/providers")
def providers(_: str = Depends(require_login)):
    return ProviderRegistry.states()


@app.get("/api/jobs")
def jobs(limit: int = Query(default=50, ge=1, le=200), _: str = Depends(require_login)):
    return store.list(limit)


@app.post("/api/intake", status_code=status.HTTP_201_CREATED)
def intake(payload: IntakeRequest, _: str = Depends(require_login)):
    try:
        provider = ProviderRegistry.detect(str(payload.share_url))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    job_status = JobStatus.QUEUED if provider.configured else JobStatus.WAITING_AUTH
    return store.create(
        kind="share_intake",
        provider=provider.name.value,
        title=payload.title,
        status=job_status.value,
        detail={
            "share_url": str(payload.share_url),
            "target_folder": payload.target_folder,
            "auto_organize": payload.auto_organize,
            "message": "已进入处理队列" if provider.configured else f"请先配置 {provider.credential_env}",
        },
    )


@app.get("/api/tmdb/search")
async def tmdb_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        return await search_tmdb(settings, q)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"TMDB查询失败：{error}") from error


@app.get("/api/search")
async def resource_search(q: str = Query(min_length=1, max_length=100), _: str = Depends(require_login)):
    try:
        return await search_resources(settings, q)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"资源搜索失败：{error}") from error


@app.get("/api/subscriptions")
def subscriptions(_: str = Depends(require_login)):
    return store.list_subscriptions()


@app.post("/api/subscriptions", status_code=status.HTTP_201_CREATED)
def create_subscription(payload: SubscriptionRequest, _: str = Depends(require_login)):
    return store.create_subscription(payload.keyword, payload.auto_intake)


@app.post("/api/strm", status_code=status.HTTP_201_CREATED)
def create_strm(payload: StrmRequest, _: str = Depends(require_login)):
    target = generate_strm(settings, payload.relative_dir, payload.name, str(payload.play_url))
    return {"created": True, "path": str(target.relative_to(settings.strm_dir))}


@app.post("/api/notify")
async def notify(payload: NotifyRequest, _: str = Depends(require_login)):
    try:
        delivered = await send_notifications(settings, payload.message)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"通知发送失败：{error}") from error
    return {"delivered": delivered, "configured": bool(delivered)}
