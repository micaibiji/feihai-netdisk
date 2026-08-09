from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class ProviderName(StrEnum):
    CHINA_MOBILE = "china_mobile"
    QUARK = "quark"
    BAIDU = "baidu"
    PAN115 = "115"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELED = "canceled"
    WAITING_AUTH = "waiting_auth"
    COMPLETED = "completed"
    FAILED = "failed"


class ValidationState(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    UNVERIFIABLE = "unverifiable"


class AuthSessionState(StrEnum):
    WAITING = "waiting"
    SCANNED = "scanned"
    SUCCEEDED = "succeeded"
    EXPIRED = "expired"
    CANCELED = "canceled"
    FAILED = "failed"


class IntakeRequest(BaseModel):
    share_url: HttpUrl
    title: str = Field(min_length=1, max_length=200)
    target_folder: str = Field(default="影视", max_length=300)
    auto_organize: bool = True


class StrmRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    play_url: HttpUrl
    relative_dir: str = Field(default="未分类", max_length=300)


class NotifyRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class SubscriptionRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    auto_intake: bool = True
    media_type: str = Field(default="tv", pattern="^(movie|tv|anime|variety|documentary)$")
    year: int | None = Field(default=None, ge=1900, le=2200)


class SubscriptionSourceRequest(BaseModel):
    share_url: HttpUrl
    title: str = Field(default="", max_length=300)
    season: int = Field(default=1, ge=0, le=200)
    episode: int = Field(default=0, ge=0, le=10000)
    quality: str = Field(default="", max_length=80)
    source: str = Field(default="manual", max_length=120)


class SettingsRequest(BaseModel):
    telegram_enabled: bool = True
    auto_metadata: bool = True
    auto_subtitles: bool = True
    auto_organize: bool = True
    fnos_library_path: str = Field(default="/app/strm", max_length=500)
    naming_language: str = Field(default="zh-CN", max_length=20)


class TmdbSettingsRequest(BaseModel):
    api_key: str = Field(default="", max_length=500)
    language: str = Field(default="zh-CN", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    region: str = Field(default="CN", pattern=r"^[A-Z]{2}$")


class PansouSettingsRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=500, pattern=r"^https?://")
    api_path: str = Field(default="/api/search", min_length=2, max_length=200, pattern=r"^/")
    source: str = Field(default="all", pattern=r"^(all|tg|plugin)$")
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    token: str = Field(default="", max_length=4000)
    clear_credentials: bool = False


class CheckerSettingsRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=500, pattern=r"^https?://")
    api_path: str = Field(default="/api/v1/links/check", min_length=2, max_length=200, pattern=r"^/")
    token: str = Field(default="", max_length=4000)
    timeout_seconds: int = Field(default=35, ge=5, le=120)
    cache_minutes: int = Field(default=120, ge=0, le=10080)
    clear_token: bool = False


class ResourceValidationRequest(BaseModel):
    share_url: HttpUrl
    force: bool = False


class DirectoryRequest(BaseModel):
    path: str = Field(default="/", max_length=1000)


class PublicDirectoryEntry(BaseModel):
    provider: ProviderName
    path: str = Field(min_length=1, max_length=1000)
    label: str = Field(default="", max_length=100)


class PublicDirectoriesRequest(BaseModel):
    entries: list[PublicDirectoryEntry] = Field(default_factory=list, max_length=30)


class PublicDirectoryBrowseRequest(BaseModel):
    path: str = Field(default="", max_length=1000)


class ProviderCredentialRequest(BaseModel):
    credential: str = Field(min_length=6, max_length=12000)
    account_mask: str = Field(default="已授权账号", max_length=100)


class ResourceResult(BaseModel):
    provider: ProviderName
    title: str
    url: HttpUrl
    source: str = "unknown"
    datetime: str | None = None
