from __future__ import annotations

from urllib.parse import urlparse

from .base import CloudAdapter, CloudError
from .baidu import BaiduAdapter
from .mobile import MobileAdapter
from .pan115 import Pan115Adapter
from .quark import QuarkAdapter


class ProviderRegistry:
    classes: dict[str, type[CloudAdapter]] = {
        "baidu": BaiduAdapter,
        "quark": QuarkAdapter,
        "115": Pan115Adapter,
        "china_mobile": MobileAdapter,
    }
    labels = {
        "baidu": "百度网盘",
        "quark": "夸克网盘",
        "115": "115网盘",
        "china_mobile": "中国移动云盘",
        "magnet": "磁力资源",
    }
    domains = {
        "baidu": ("pan.baidu.com",),
        "quark": ("pan.quark.cn", "drive.quark.cn"),
        "115": ("115.com", "115cdn.com", "anxia.com"),
        "china_mobile": ("yun.139.com", "caiyun.139.com"),
    }

    @classmethod
    def create(cls, provider: str, credential: str) -> CloudAdapter:
        try:
            adapter = cls.classes[provider]
        except KeyError as error:
            raise CloudError("暂不支持这个网盘") from error
        if not credential:
            raise CloudError(f"请先授权{cls.labels[provider]}")
        return adapter(credential)

    @classmethod
    def detect(cls, url: str) -> str:
        if url.lower().startswith("magnet:?xt=urn:btih:"):
            return "magnet"
        host = (urlparse(url).hostname or "").lower()
        for provider, domains in cls.domains.items():
            if any(host == domain or host.endswith("." + domain) for domain in domains):
                return provider
        raise CloudError("只支持百度、夸克、115、中国移动云盘和磁力链接")
