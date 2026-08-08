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
    WAITING_AUTH = "waiting_auth"
    COMPLETED = "completed"
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


class ResourceResult(BaseModel):
    provider: ProviderName
    title: str
    url: HttpUrl
    source: str = "unknown"
    datetime: str | None = None
