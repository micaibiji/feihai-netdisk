from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str
    admin_username: str
    admin_password: str
    data_dir: Path
    strm_dir: Path
    tmdb_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    wecom_webhook_url: str
    pansou_base_url: str
    provider_priority: tuple[str, ...]
    subscription_interval_seconds: int
    public_base_url: str = ""
    baidu_client_id: str = ""
    baidu_redirect_uri: str = ""
    native_mount_base: Path = Path("/mnt/netdisk")
    native_mount_providers: tuple[str, ...] = ()

    def native_mount_path(self, provider: str) -> Path:
        return self.native_mount_base / provider

    def native_mount_enabled(self, provider: str) -> bool:
        return provider in self.native_mount_providers

    @property
    def database_path(self) -> Path:
        return self.data_dir / "feihai.db"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "飞海网盘"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-me-now"),
        data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
        strm_dir=Path(os.getenv("STRM_DIR", "strm")).resolve(),
        tmdb_api_key=os.getenv("TMDB_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        wecom_webhook_url=os.getenv("WECOM_WEBHOOK_URL", ""),
        pansou_base_url=os.getenv("PANSOU_BASE_URL", "").rstrip("/"),
        provider_priority=tuple(
            item.strip()
            for item in os.getenv("PROVIDER_PRIORITY", "115,baidu,quark,china_mobile").split(",")
            if item.strip()
        ),
        subscription_interval_seconds=max(
            300, int(os.getenv("SUBSCRIPTION_INTERVAL_SECONDS", "1800"))
        ),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        baidu_client_id=os.getenv("BAIDU_CLIENT_ID", ""),
        baidu_redirect_uri=os.getenv("BAIDU_REDIRECT_URI", ""),
        native_mount_base=Path(os.getenv("NATIVE_MOUNT_BASE", "/mnt/netdisk")).resolve(),
        native_mount_providers=tuple(
            item.strip()
            for item in os.getenv("NATIVE_MOUNT_PROVIDERS", "").split(",")
            if item.strip()
        ),
    )
