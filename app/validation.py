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
