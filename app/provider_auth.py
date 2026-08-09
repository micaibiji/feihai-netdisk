from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx


class ProviderAuthError(RuntimeError):
    pass


async def start_115_qr() -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a 115 QR session through the official 115 QR endpoints."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/")
        response.raise_for_status()
        body = response.json()
    data = body.get("data") or {}
    uid = str(data.get("uid") or "")
    if not uid or not data.get("time") or not data.get("sign"):
        raise ProviderAuthError("115 未返回可用的二维码，请稍后重试")
    secret = {"uid": uid, "time": data["time"], "sign": data["sign"]}
    public = {
        "qr_image_url": f"https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?{urlencode({'uid': uid})}",
        "message": "请使用 115 手机客户端扫码并确认登录",
    }
    return public, secret


def parse_115_qr_state(body: dict[str, Any]) -> tuple[str, str]:
    data = body.get("data") or {}
    qr_status = data.get("status")
    # Before the QR code is scanned, 115 currently answers with
    # {"state": 1, "code": 0, "data": {}} after its long poll.  The
    # absence of data.status therefore means "still waiting", not failure.
    if qr_status is None and body.get("state") == 1 and body.get("code") == 0:
        return "waiting", "等待扫码"
    try:
        qr_status = int(qr_status)
    except (TypeError, ValueError):
        raise ProviderAuthError(body.get("message") or "115 返回了无法识别的扫码状态")
    states = {
        0: ("waiting", "等待扫码"),
        1: ("scanned", "已扫码，请在手机上确认"),
        2: ("confirmed", "已确认，正在完成授权"),
        -1: ("expired", "二维码已过期，请刷新"),
        -2: ("canceled", "已在手机上取消"),
    }
    try:
        return states[qr_status]
    except KeyError as error:
        raise ProviderAuthError(body.get("message") or "115 返回了无法识别的扫码状态") from error


async def poll_115_qr(secret: dict[str, Any]) -> tuple[str, str, str]:
    """Return (state, user-facing message, encrypted credential if completed)."""
    params = {key: secret[key] for key in ("uid", "time", "sign")}
    timeout = httpx.Timeout(15, read=40)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            response = await client.get("https://qrcodeapi.115.com/get/status/", params=params)
        except httpx.ReadTimeout:
            # 115 uses a long-poll response. A quiet poll means the QR is still waiting,
            # not that authorization failed.
            return "waiting", "等待扫码", ""
        response.raise_for_status()
        body = response.json()
        auth_state, message = parse_115_qr_state(body)
        if auth_state != "confirmed":
            return auth_state, message, ""
        login = await client.post(
            "https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/",
            data={"account": secret["uid"]},
        )
        login.raise_for_status()
        login_data = (login.json().get("data") or {}).get("cookie") or {}
    if not login_data:
        raise ProviderAuthError("扫码已确认，但 115 未返回登录信息")
    credential = "; ".join(f"{key}={value}" for key, value in login_data.items())
    return "succeeded", "授权成功", credential


def serialize_secret(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def deserialize_secret(value: str) -> dict[str, Any]:
    try:
        output = json.loads(value)
    except json.JSONDecodeError as error:
        raise ProviderAuthError("授权会话已损坏，请重新扫码") from error
    if not isinstance(output, dict):
        raise ProviderAuthError("授权会话已损坏，请重新扫码")
    return output
