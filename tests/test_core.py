from __future__ import annotations

import base64
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import integrations
from app.providers.baidu import BaiduAdapter
from app.providers.base import BrowserSupport, FolderEntry, ShareFile, browser_support
from app.providers.mobile import MobileAdapter
from app.providers.pan115 import Pan115Adapter
from app.providers.quark import QuarkAdapter
from app.providers.registry import ProviderRegistry
from app.storage import Store
from app.vault import CredentialVault


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://pan.baidu.com/s/abc", "baidu"),
        ("https://pan.quark.cn/s/abc", "quark"),
        ("https://115.com/s/abc", "115"),
        ("https://yun.139.com/share/abc", "china_mobile"),
    ],
)
def test_provider_detection(url: str, provider: str) -> None:
    assert ProviderRegistry.detect(url) == provider


def test_mobile_directory_detection_does_not_treat_file_type_one_as_folder() -> None:
    assert MobileAdapter._is_dir({"fileType": 0}) is True
    assert MobileAdapter._is_dir({"fileType": 1}) is False
    assert MobileAdapter._is_dir({"isFolder": True}) is True


@pytest.mark.parametrize(
    ("name", "playable"),
    [
        ("电影.1080p.H264.AAC.mp4", True),
        ("电影.webm", True),
        ("电影.HEVC.mp4", False),
        ("电影.Dolby.Vision.mp4", False),
        ("电影.H264.mkv", False),
        ("电影.mp3", False),
    ],
)
def test_browser_support_is_conservative(name: str, playable: bool) -> None:
    assert browser_support(name).playable is playable


def test_share_url_parsers() -> None:
    assert QuarkAdapter.parse_share("https://pan.quark.cn/s/abc123", "") == ("abc123", "")
    assert Pan115Adapter.parse_share("https://115.com/s/xyz", "1234") == ("xyz", "1234")
    assert BaiduAdapter.parse_share("https://pan.baidu.com/s/1abc", "") == ("1abc", "abc", "")


def test_mobile_token_parses_account() -> None:
    token = base64.b64encode(b"client:13800138000:secret").decode()
    adapter = MobileAdapter(token)
    assert adapter.account == "13800138000"


def test_vault_encrypts_plaintext(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path)
    vault.save("quark", "cookie=secret")
    assert vault.load("quark") == "cookie=secret"
    assert b"cookie=secret" not in (tmp_path / "credentials" / "quark.token").read_bytes()


def test_store_uses_new_prefixed_schema(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    assert {item["provider"] for item in store.accounts()} == {"baidu", "quark", "115", "china_mobile"}
    with store.connect() as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fh_accounts" in names
    assert all(name.startswith("fh_") or name == "sqlite_sequence" for name in names)


def test_temp_last_played_can_be_extended(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now = datetime.now(UTC)
    store.add_temp(
        {
            "id": "one",
            "provider": "quark",
            "title": "测试",
            "share_url": "https://pan.quark.cn/s/abc",
            "cloud_file_id": "f1",
            "file_name": "测试.mp4",
            "last_played_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=48)).isoformat(),
            "created_at": now.isoformat(),
        }
    )
    later = now + timedelta(hours=2)
    store.touch_temp("one", later.isoformat(), (later + timedelta(hours=48)).isoformat())
    assert store.temp("one")["last_played_at"] == later.isoformat()


class FakeResponse:
    def __init__(self, body, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._body


class FakeClient:
    response = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, *args, **kwargs):
        return FakeResponse(self.response)

    async def post(self, *args, **kwargs):
        return FakeResponse(self.response)


def test_pansou_parsing_keeps_all_four_providers(monkeypatch) -> None:
    FakeClient.response = {
        "results": [
            {"title": "作品 1080P", "url": "https://pan.baidu.com/s/abc"},
            {"title": "作品 4K", "url": "https://pan.quark.cn/s/def"},
            {"title": "作品 S01E02", "url": "https://115.com/s/ghi"},
            {"title": "作品", "url": "https://yun.139.com/share/jkl"},
        ]
    }
    monkeypatch.setattr(integrations.httpx, "AsyncClient", FakeClient)
    items = asyncio.run(integrations.search_pansou("http://pansou", "作品"))
    assert {item["provider"] for item in items} == {"baidu", "quark", "115", "china_mobile"}
    assert len({item["fingerprint"] for item in items}) == 4


def test_checker_only_marks_explicit_invalid(monkeypatch) -> None:
    good = "https://pan.baidu.com/s/good"
    bad = "https://pan.quark.cn/s/bad"
    unknown = "https://115.com/s/unknown"
    FakeClient.response = {"valid_links": [good], "invalid_links": [bad], "pending_links": []}
    monkeypatch.setattr(integrations.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(integrations.check_links("http://checker", [good, bad, unknown]))
    assert result[good]["state"] == "valid"
    assert result[bad]["state"] == "invalid"
    assert result[unknown]["state"] == "unverifiable"


@pytest.fixture
def web_client(tmp_path: Path, monkeypatch):
    import app.main as main

    store = Store(tmp_path / "web.db")
    vault = CredentialVault(tmp_path)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "vault", vault)
    main._inspect_cache.clear()
    with TestClient(main.app) as client:
        yield client, main


def test_public_health_and_admin_boundary(web_client) -> None:
    client, main = web_client
    health = client.get("/api/health").json()
    assert health["version"] == "1.0.3"
    assert health["port_policy"] == "single-port"
    assert client.get("/api/admin/overview").status_code == 401
    response = client.post(
        "/api/login",
        json={"username": main.settings.admin_username, "password": main.settings.admin_password},
    )
    assert response.status_code == 200
    assert client.get("/api/admin/overview").status_code == 200


def test_constant_time_comparison_accepts_chinese_credentials(web_client) -> None:
    _, main = web_client
    assert main._safe_equal("请改成一个强密码", "请改成一个强密码") is True
    assert main._safe_equal("请改成一个强密码", "另一个密码") is False


def test_static_page_has_guest_copy_and_no_public_directory(web_client) -> None:
    client, _ = web_client
    html = client.get("/").text
    assert "复制链接" in html
    assert "公开目录" not in html
    assert "OpenList" not in html


def test_compose_exposes_only_product_port() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"12366:12366"' in compose
    assert "5244" not in compose
    assert compose.count(":12366") == 1


def test_frontend_has_only_one_delegated_click_handler() -> None:
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert script.count("document.addEventListener('click'") == 1


def test_mobile_layout_has_compact_navigation_and_filters() -> None:
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "@media(max-width:600px)" in css
    assert "grid-template-columns:repeat(auto-fit,minmax(92px,1fr))" in css
    assert ".resource-filters strong{flex:0 0 100%" in css
    assert ".resource-grid .resource-actions{grid-template-columns:repeat(2,minmax(0,1fr))" in css
