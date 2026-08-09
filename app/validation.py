from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .providers import ProviderRegistry


@dataclass(frozen=True)
class ValidationResult:
    state: str
    reason: str
    checked_at: str
    recheck_after: str


INVALID_MARKERS = (
    "分享已取消", "分享已失效", "分享不存在", "链接已失效", "页面不存在",
    "来晚了，该分享文件已过期", "分享的文件已经被删除", "此分享已被取消",
)

PANCHECK_PROVIDER_NAMES = {
    "115": "pan115",
    "baidu": "baidu",
    "quark": "quark",
    "china_mobile": "cmcc",
}


class ExternalValidatorError(RuntimeError):
    pass


def should_show_resource(state: str) -> bool:
    """Only an explicit invalid result is hidden; every other checker state remains visible."""
    return state != "invalid"


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _canonical_url(url: str) -> str:
    return url.strip().rstrip("/")


async def test_checker_connection(base_url: str, token: str = "") -> dict[str, str | int | bool]:
    base_url = base_url.rstrip("/")
    if not base_url:
        raise ExternalValidatorError("尚未填写检测网站地址")
    try:
        async with httpx.AsyncClient(
            timeout=12, follow_redirects=True, headers=_auth_headers(token),
        ) as client:
            for path in ("/api/v1/health", "/health", "/"):
                response = await client.get(f"{base_url}{path}")
                if response.status_code in {401, 403}:
                    response.raise_for_status()
                if response.status_code < 500 and response.status_code != 404:
                    return {"connected": True, "endpoint": path, "status_code": response.status_code}
    except httpx.HTTPError as error:
        raise ExternalValidatorError(f"检测网站无法访问：{type(error).__name__}") from error
    raise ExternalValidatorError("检测网站没有返回可用的健康状态")


async def validate_share_urls(
    urls: list[str], *, base_url: str, api_path: str = "/api/v1/links/check",
    token: str = "", timeout_seconds: int = 35, cache_minutes: int = 120,
) -> dict[str, ValidationResult]:
    """Validate a batch through a user-owned PanCheck-compatible service."""
    if not base_url:
        raise ExternalValidatorError("尚未在设置中连接自己的检测网站")
    unique_urls = list(dict.fromkeys(str(url) for url in urls if str(url).strip()))
    if not unique_urls:
        return {}
    platforms: list[str] = []
    for url in unique_urls:
        try:
            provider = ProviderRegistry.detect(url)
        except ValueError:
            continue
        name = PANCHECK_PROVIDER_NAMES.get(provider.name.value)
        if name and name not in platforms:
            platforms.append(name)
    payload = {"links": unique_urls, "selectedPlatforms": platforms}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}{api_path}", json=payload, headers=_auth_headers(token),
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ExternalValidatorError(f"检测服务暂时不可用：{type(error).__name__}") from error
    if isinstance(body.get("data"), dict):
        body = body["data"]
    valid = {_canonical_url(str(item)) for item in body.get("valid_links", [])}
    invalid = {_canonical_url(str(item)) for item in body.get("invalid_links", [])}
    pending = {_canonical_url(str(item)) for item in body.get("pending_links", [])}
    if not any((valid, invalid, pending)) and unique_urls:
        raise ExternalValidatorError("检测网站返回了无法识别的结果格式")
    now = datetime.now(UTC)
    output: dict[str, ValidationResult] = {}
    for url in unique_urls:
        key = _canonical_url(url)
        if key in valid:
            output[url] = _result("valid", "你的检测网站确认链接有效", now, minutes=cache_minutes)
        elif key in invalid:
            output[url] = _result("invalid", "你的检测网站确认链接已失效", now, minutes=cache_minutes)
        elif key in pending:
            output[url] = _result("unverifiable", "检测网站暂未完成检查", now, minutes=15)
        else:
            output[url] = _result("unverifiable", "检测网站未返回该链接的结果", now, minutes=15)
    return output


async def validate_share_url(url: str) -> ValidationResult:
    """Conservative public-page probe: only explicit provider responses become invalid."""
    ProviderRegistry.detect(url)
    now = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 FeihaiNetdisk/0.4 (+local NAS)"},
        ) as client:
            response = await client.get(url)
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        return _result("unverifiable", f"网络暂时不可用：{type(error).__name__}", now, minutes=15)
    if response.status_code in {404, 410}:
        return _result("invalid", f"网盘明确返回 HTTP {response.status_code}", now, hours=12)
    if response.status_code in {401, 403, 409, 423, 429} or response.status_code >= 500:
        return _result("unverifiable", f"网盘暂时拒绝验证（HTTP {response.status_code}）", now, minutes=30)
    text = response.text[:500_000]
    marker = next((item for item in INVALID_MARKERS if item in text), "")
    if marker:
        return _result("invalid", f"页面明确提示：{marker}", now, hours=12)
    if 200 <= response.status_code < 400:
        return _result("valid", "分享页面可以正常访问", now, hours=2)
    return _result("unverifiable", f"无法确认的响应（HTTP {response.status_code}）", now, minutes=30)


def _result(state: str, reason: str, now: datetime, *, minutes: int = 0, hours: int = 0) -> ValidationResult:
    return ValidationResult(
        state=state,
        reason=reason,
        checked_at=now.isoformat(),
        recheck_after=(now + timedelta(minutes=minutes, hours=hours)).isoformat(),
    )
