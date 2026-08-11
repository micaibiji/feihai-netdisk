from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class ProviderName(StrEnum):
    BAIDU = "baidu"
    QUARK = "quark"
    PAN115 = "115"
    CHINA_MOBILE = "china_mobile"
    MAGNET = "magnet"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=300)


class CredentialRequest(BaseModel):
    credential: str = Field(min_length=6, max_length=20000)
    kind: str = Field(default="auto", pattern=r"^(auto|cookie|token|oauth)$")
    account_label: str = Field(default="已授权账号", max_length=80)


class DirectoryRequest(BaseModel):
    parent_id: str = Field(default="", max_length=1000)
    parent_path: str = Field(default="/", max_length=1500)


class CreateFolderRequest(BaseModel):
    parent_id: str = Field(default="", max_length=1000)
    parent_path: str = Field(default="/", max_length=1500)
    name: str = Field(min_length=1, max_length=180)


class ResourceInspectRequest(BaseModel):
    provider: ProviderName
    share_url: HttpUrl
    extraction_code: str = Field(default="", max_length=20)


class TransferRequest(ResourceInspectRequest):
    title: str = Field(min_length=1, max_length=300)
    target_id: str = Field(default="", max_length=1000)
    target_path: str = Field(default="/", max_length=1500)
    selected_file_ids: list[str] = Field(default_factory=list, max_length=1000)
    scope: str = Field(default="all", pattern=r"^(single|season|all)$")
    duplicate_policy: str = Field(default="skip", pattern=r"^(skip|keep_both)$")


class PreparePlayRequest(ResourceInspectRequest):
    title: str = Field(min_length=1, max_length=300)
    file_id: str = Field(default="", max_length=1000)
    media_type: str = Field(default="unknown", pattern=r"^(movie|tv|unknown)$")


class MagnetInspectRequest(BaseModel):
    magnet_url: str = Field(min_length=20, max_length=8000, pattern=r"(?i)^magnet:\?xt=urn:btih:")


class MagnetPrepareRequest(MagnetInspectRequest):
    title: str = Field(min_length=1, max_length=300)
    file_id: str = Field(min_length=1, max_length=20)


class KeepTemporaryRequest(BaseModel):
    target_id: str = Field(default="", max_length=1000)
    target_path: str = Field(default="/", max_length=1500)
    duplicate_policy: str = Field(default="skip", pattern=r"^(skip|keep_both)$")


class IntegrationSettingsRequest(BaseModel):
    pansou_url: str = Field(default="", max_length=500)
    checker_url: str = Field(default="", max_length=500)
    tmdb_api_key: str = Field(default="", max_length=500)
    telegram_bot_token: str = Field(default="", max_length=500)
    telegram_chat_id: str = Field(default="", max_length=100)


class SubscriptionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="tv", pattern=r"^(movie|tv|anime)$")
    year: int | None = Field(default=None, ge=1900, le=2200)
