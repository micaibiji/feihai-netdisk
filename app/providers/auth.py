from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


class ProviderAuthError(RuntimeError):
    pass


async def start_115_qr() -> tuple[dict[str, str], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/")
        response.raise_for_status()
        data = (response.json().get("data") or {})
    uid = str(data.get("uid") or "")
    if not uid or not data.get("time") or not data.get("sign"):
        raise ProviderAuthError("115 没有返回可用二维码，请稍后重试")
    secret = {"uid": uid, "time": data["time"], "sign": data["sign"]}
    return {
        "qr_image_url": f"https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?{urlencode({'uid': uid})}",
        "message": "请用 115 手机客户端扫码，并在手机上确认登录",
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
            "https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/",
            data={"account": secret["uid"]},
        )
        login.raise_for_status()
        cookie = (login.json().get("data") or {}).get("cookie") or {}
    if not cookie:
        raise ProviderAuthError("扫码已确认，但 115 没有返回登录凭证")
    return "succeeded", "授权成功", "; ".join(f"{key}={value}" for key, value in cookie.items())
