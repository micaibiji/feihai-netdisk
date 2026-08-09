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
    pansou_url: str
    checker_url: str
    tmdb_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    public_base_url: str
    temp_retention_hours: int
    temp_folder_name: str
    cleanup_interval_seconds: int

    @property
    def database_path(self) -> Path:
        return self.data_dir / "feihai-v1.db"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "飞海网盘"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "change-me-now"),
        data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
        pansou_url=os.getenv("PANSOU_URL", "").rstrip("/"),
        checker_url=os.getenv("CHECKER_URL", "").rstrip("/"),
        tmdb_api_key=os.getenv("TMDB_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        temp_retention_hours=max(1, int(os.getenv("TEMP_RETENTION_HOURS", "48"))),
        temp_folder_name=os.getenv("TEMP_FOLDER_NAME", "影视临时播放").strip() or "影视临时播放",
        cleanup_interval_seconds=max(60, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600"))),
    )
