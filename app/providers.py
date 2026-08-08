from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import ProviderName


@dataclass(frozen=True)
class Provider:
    name: ProviderName
    label: str
    domains: tuple[str, ...]
    credential_env: str

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == domain or host.endswith(f".{domain}") for domain in self.domains)

    @property
    def configured(self) -> bool:
        return bool(os.getenv(self.credential_env, "").strip())


PROVIDERS = (
    Provider(
        ProviderName.CHINA_MOBILE,
        "中国移动云盘",
        ("yun.139.com", "caiyun.139.com"),
        "CHINA_MOBILE_TOKEN",
    ),
    Provider(
        ProviderName.QUARK,
        "夸克网盘",
        ("pan.quark.cn", "drive.quark.cn"),
        "QUARK_COOKIE",
    ),
    Provider(
        ProviderName.BAIDU,
        "百度网盘",
        ("pan.baidu.com",),
        "BAIDU_ACCESS_TOKEN",
    ),
    Provider(
        ProviderName.PAN115,
        "115网盘",
        ("115.com", "115cdn.com"),
        "115_ACCESS_TOKEN",
    ),
)


class ProviderRegistry:
    @staticmethod
    def detect(url: str) -> Provider:
        for provider in PROVIDERS:
            if provider.matches(url):
                return provider
        raise ValueError("暂不支持这个分享链接，请使用移动、夸克、百度或115网盘链接")

    @staticmethod
    def states() -> list[dict[str, str | bool]]:
        return [
            {
                "name": provider.name.value,
                "label": provider.label,
                "configured": provider.configured,
            }
            for provider in PROVIDERS
        ]

    @staticmethod
    def label(name: str) -> str:
        for provider in PROVIDERS:
            if provider.name.value == name:
                return provider.label
        return name
