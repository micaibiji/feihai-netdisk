from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


class ProviderAuthError(RuntimeError):
    pass


PAN115_QR_APP = "alipaymini"
PAN115_QR_TOKEN_URL = f"https://qrcodeapi.115.com/api/1.0/{PAN115_QR_APP}/1.0/token/"


def pan115_qr_image_url(uid: str) -> str:
    return f"https://qrcodeapi.115.com/api/1.0/{PAN115_QR_APP}/1.0/qrcode?{urlencode({'uid': uid})}"


def pan115_qr_login_url() -> str:
    return f"https://passportapi.115.com/app/1.0/{PAN115_QR_APP}/1.0/login/qrcode/"


async def start_115_qr() -> tuple[dict[str, str], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(PAN115_QR_TOKEN_URL)
        response.raise_for_status()
        data = (response.json().get("data") or {})
    uid = str(data.get("uid") or "")
    if not uid or not data.get("time") or not data.get("sign"):
        raise ProviderAuthError("115 没有返回可用二维码，请稍后重试")
    secret = {"uid": uid, "time": data["time"], "sign": data["sign"]}
    return {
        "qr_image_url": pan115_qr_image_url(uid),
        "message": "请用 115 手机 App 扫码确认；本次将绑定为支付宝小程序端",
    }, secret


async def poll_115_qr(secret: dict[str, Any]) -> tuple[str, str, str]:
    timeout = httpx.Timeout(15, read=40)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            response = await client.get(
                "https://qrcodeapi.115.com/get/status/",
                params={key: secret[key] for key in ("uid", "time", "sign")},
            )
        except httpx.ReadTimeout:
            return "waiting", "等待扫码", ""
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        status = data.get("status")
        if status is None and body.get("state") == 1 and body.get("code") == 0:
            return "waiting", "等待扫码", ""
        states = {
            0: ("waiting", "等待扫码"),
            1: ("scanned", "已扫码，请在手机上确认"),
            -1: ("expired", "二维码已过期，请刷新"),
            -2: ("canceled", "已取消授权"),
        }
        try:
            numeric = int(status)
        except (TypeError, ValueError) as error:
            raise ProviderAuthError(body.get("message") or "115 返回了无法识别的扫码状态") from error
        if numeric != 2:
            return (*states.get(numeric, ("waiting", "等待扫码")), "")
        login = await client.post(
            pan115_qr_login_url(),
            data={"app": PAN115_QR_APP, "account": secret["uid"]},
        )
        login.raise_for_status()
        cookie = (login.json().get("data") or {}).get("cookie") or {}
    if not cookie:
        raise ProviderAuthError("扫码已确认，但 115 没有返回登录凭证")
    return "succeeded", "授权成功", "; ".join(f"{key}={value}" for key, value in cookie.items())
