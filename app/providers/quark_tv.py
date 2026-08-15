from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import httpx

from .base import AuthenticationError, CloudError


API = "https://open-api-drive.quark.cn"
TOKEN_API = "https://api.extscreen.com/quarkdrive/token"
CLIENT_ID = "d3194e61504e493eb6222857bccfed94"
SIGN_KEY = "kw2dvtd7p4t3pjl2d9ed9yc8yej8kw2d"
APP_VERSION = "1.8.2.2"
CHANNEL = "GENERAL"
USER_AGENT = (
    "Mozilla/5.0 (Linux; U; Android 13; zh-cn; M2004J7AC Build/UKQ1.231108.001) "
    "AppleWebKit/533.1 (KHTML, like Gecko) Mobile Safari/533.1"
)
DEVICE = {
    "device_brand": "Xiaomi",
    "platform": "tv",
    "device_name": "M2004J7AC",
    "device_model": "M2004J7AC",
    "build_device": "M2004J7AC",
    "build_product": "M2004J7AC",
    "device_gpu": "Adreno (TM) 550",
    "activity_rect": "{}",
    "channel": CHANNEL,
}


def _signature(method: str, path: str, device_id: str) -> tuple[str, str, str]:
    timestamp = str(int(time.time() * 1000))
    request_id = hashlib.md5(f"{device_id}{timestamp}".encode()).hexdigest()
    token = hashlib.sha256(f"{method}&{path}&{timestamp}&{SIGN_KEY}".encode()).hexdigest()
    return timestamp, token, request_id


async def _request(
    method: str,
    path: str,
    device_id: str,
    access_token: str = "",
    params: dict[str, Any] | None = None,
    tolerate_error: bool = False,
) -> dict[str, Any]:
    timestamp, token, request_id = _signature(method, path, device_id)
    query: dict[str, Any] = {
        "req_id": request_id,
        "access_token": access_token,
        "app_ver": APP_VERSION,
        "device_id": device_id,
        **DEVICE,
        **(params or {}),
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "x-pan-tm": timestamp,
        "x-pan-token": token,
        "x-pan-client-id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.request(method, f"{API}{path}", params=query, headers=headers)
    try:
        body = response.json()
    except ValueError as error:
        raise CloudError("夸克电视端返回了无法识别的内容") from error
    errno = int(body.get("errno") or 0)
    status = int(body.get("status") or 0)
    if response.is_error or errno or status >= 400:
        if tolerate_error:
            return body
        message = body.get("error_info") or body.get("message") or response.reason_phrase
        raise AuthenticationError(f"夸克电视端授权失败：{message}")
    return body


async def start_quark_tv_qr(existing_device_id: str = "") -> tuple[dict[str, str], dict[str, str]]:
    # A new device id creates another TV device at Quark.  Reusing the id
    # already bound to this NAS lets a renewed QR authorization replace the
    # credentials for the same device instead of consuming another slot.
    device_id = existing_device_id.strip() or hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()
    body = await _request(
        "GET",
        "/oauth/authorize",
        device_id,
        params={
            "auth_type": "code",
            "client_id": CLIENT_ID,
            "scope": "netdisk",
            "qrcode": "1",
            "qr_width": "460",
            "qr_height": "460",
        },
    )
    qr_data = str(body.get("qr_data") or "")
    query_token = str(body.get("query_token") or "")
    if not qr_data or not query_token:
        raise CloudError("夸克没有返回电视端登录二维码")
    return (
        {
            "qr_image_url": f"data:image/png;base64,{qr_data}",
            "message": "请使用夸克手机 App 扫码并确认；此授权只用于云端转码播放。",
        },
        {"device_id": device_id, "query_token": query_token},
    )


async def _exchange(device_id: str, value: str, refresh: bool) -> dict[str, str]:
    _, _, request_id = _signature("POST", "/token", device_id)
    payload = {
        "req_id": request_id,
        "app_ver": APP_VERSION,
        "device_id": device_id,
        **DEVICE,
        ("refresh_token" if refresh else "code"): value,
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        response = await client.post(TOKEN_API, json=payload)
    response.raise_for_status()
    body = response.json()
    data = body.get("data") or {}
    if int(body.get("code") or 0) != 200 or not data.get("refresh_token"):
        raise AuthenticationError(str(body.get("message") or "夸克电视端没有返回登录凭证"))
    return {
        "refresh_token": str(data["refresh_token"]),
        "access_token": str(data.get("access_token") or ""),
    }


async def poll_quark_tv_qr(secret: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    body = await _request(
        "GET",
        "/oauth/code",
        secret["device_id"],
        params={
            "client_id": CLIENT_ID,
            "scope": "netdisk",
            "query_token": secret["query_token"],
        },
        tolerate_error=True,
    )
    code = str(body.get("code") or "")
    if not code:
        message = str(body.get("error_info") or body.get("message") or "等待扫码")
        if "过期" in message or "expired" in message.lower():
            return "expired", "二维码已过期，请刷新", {}
        return "waiting", "等待扫码或手机确认", {}
    tokens = await _exchange(secret["device_id"], code, False)
    return "succeeded", "夸克电视端播放授权成功", {
        "tv_device_id": secret["device_id"],
        "tv_refresh_token": tokens["refresh_token"],
    }


async def quark_tv_stream_link(fid: str, refresh_token: str, device_id: str) -> str:
    tokens = await _exchange(device_id, refresh_token, True)
    body = await _request(
        "GET",
        "/file",
        device_id,
        tokens["access_token"],
        params={
            "method": "streaming",
            "group_by": "source",
            "fid": fid,
            "resolution": "low,normal,high,super,2k,4k",
            "support": "dolby_vision",
        },
    )
    options = (body.get("data") or {}).get("video_info") or []
    priorities = {"super": 60, "high": 50, "normal": 40, "low": 30, "2k": 20, "4k": 10}
    candidates = [item for item in options if item.get("url")]
    if not candidates:
        raise CloudError("夸克电视端暂时没有生成可播放的转码视频")
    selected = max(candidates, key=lambda item: priorities.get(str(item.get("resolution") or "").lower(), 0))
    return str(selected["url"])
