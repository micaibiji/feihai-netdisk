from __future__ import annotations

import base64
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import integrations
from app.integrations import tmdb_details
from app.magnet import bdecode, magnet_info_hash, torrent_files
from app.providers.baidu import BaiduAdapter
from app.providers.base import BrowserSupport, FolderEntry, ShareFile, browser_support
from app.providers.mobile import MobileAdapter
from app.providers.pan115 import Pan115Adapter
from app.providers.auth import PAN115_QR_APP, PAN115_QR_TOKEN_URL, pan115_qr_image_url, pan115_qr_login_url
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
        ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", "magnet"),
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


def test_baidu_extracts_real_share_metadata_from_locals_mset() -> None:
    html = """<script>
    window.manifest=[{"locals":{"share":["shareid","share_uk"]}}];
    locals.mset({"shareid":54258566354,"share_uk":"1628015812","file_list":[{"fs_id":1,"server_filename":"原始目录","isdir":1}]});
    </script>"""
    meta = BaiduAdapter._json_after_marker(html, "locals.mset(")
    assert meta is not None
    assert meta["shareid"] == 54258566354
    assert meta["share_uk"] == "1628015812"
    assert meta["file_list"][0]["server_filename"] == "原始目录"


def test_baidu_merges_share_verification_cookie() -> None:
    cookie = BaiduAdapter._cookie_with_share_verification(
        "BDUSS=login; STOKEN=session; BDCLND=old", "new%2Bshare"
    )
    assert "BDUSS=login" in cookie
    assert "STOKEN=session" in cookie
    assert "BDCLND=new%2Bshare" in cookie
    assert "BDCLND=old" not in cookie


def test_quark_direct_link_forwards_cookie_and_browser_context(monkeypatch) -> None:
    adapter = QuarkAdapter(json.dumps({"cookie": "__puus=secret; kps=token"}))

    async def fake_request(*args, **kwargs):
        return {"data": [{"download_url": "https://download.example/video.mp4"}]}

    monkeypatch.setattr(adapter, "request", fake_request)
    direct = asyncio.run(adapter.direct_link(ShareFile(id="f1", name="video.mp4")))
    assert direct.headers["Cookie"] == "__puus=secret; kps=token"
    assert direct.headers["Origin"] == "https://pan.quark.cn"
    assert direct.headers["Referer"] == "https://pan.quark.cn/"
    assert "Chrome/" in direct.headers["User-Agent"]
    assert direct.redirect is False


def test_quark_prefers_browser_transcode_and_avoids_4k(monkeypatch) -> None:
    adapter = QuarkAdapter(json.dumps({"cookie": "__puus=secret; kps=token"}))

    async def fake_request(*args, **kwargs):
        assert args[1].endswith("/file/v2/play/project")
        return {
            "data": {
                "video_list": [
                    {"resolution": "4k", "video_info": {"url": "https://cdn.example/4k.mp4", "success": True}},
                    {"resolution": "super", "video_info": {"url": "https://cdn.example/1080.mp4", "success": True}},
                    {"resolution": "high", "video_info": {"url": "https://cdn.example/720.mp4", "success": True}},
                ]
            }
        }

    monkeypatch.setattr(adapter, "request", fake_request)
    direct = asyncio.run(adapter.direct_link(ShareFile(id="f1", name="video.mp4")))
    assert direct.url == "https://cdn.example/1080.mp4"
    assert direct.redirect is True
    assert direct.headers == {}


def test_quark_tv_authorization_uses_cloud_transcode(monkeypatch) -> None:
    adapter = QuarkAdapter(json.dumps({
        "cookie": "__puus=secret; kps=token",
        "tv_refresh_token": "refresh",
        "tv_device_id": "device",
    }))

    async def fake_tv_stream(fid: str, refresh_token: str, device_id: str) -> str:
        assert (fid, refresh_token, device_id) == ("f1", "refresh", "device")
        return "https://transcode.example/1080p.mp4"

    monkeypatch.setattr("app.providers.quark.quark_tv_stream_link", fake_tv_stream)
    direct = asyncio.run(adapter.direct_link(ShareFile(id="f1", name="video.mp4")))
    assert direct.url == "https://transcode.example/1080p.mp4"
    assert direct.redirect is True
    assert direct.headers == {}


def test_mobile_token_parses_account() -> None:
    token = base64.b64encode(b"client:13800138000:secret").decode()
    adapter = MobileAdapter(token)
    assert adapter.account == "13800138000"


@pytest.mark.parametrize("prefix", ["", "Basic ", "Authorization: Basic "])
def test_mobile_accepts_common_authorization_paste_formats(prefix: str) -> None:
    token = base64.b64encode(b"client:13800138000:secret").decode()
    adapter = MobileAdapter(prefix + token)
    assert adapter.token == token
    assert adapter.root_id == "/"


def test_mobile_uses_current_mcloud_signature_header() -> None:
    token = base64.b64encode(b"client:13800138000:secret").decode()
    headers = MobileAdapter(token)._headers({"parentFileId": "/"})
    timestamp, random_value, signature = headers["Mcloud-Sign"].split(",")
    assert len(timestamp) == 19
    assert len(random_value) == 16
    assert len(signature) == 32
    assert signature == signature.upper()
    assert headers["Authorization"] == f"Basic {token}"


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

    async def get(self, *args, **kwargs):
        return FakeResponse(self.response)

    async def post(self, *args, **kwargs):
        return FakeResponse(self.response)


def test_mobile_accepts_success_message_with_provider_specific_code(monkeypatch) -> None:
    token = base64.b64encode(b"client:13800138000:secret").decode()
    adapter = MobileAdapter(token)
    FakeClient.response = {
        "code": "SUC0000",
        "message": "请求成功",
        "data": {"routePolicyList": []},
    }
    monkeypatch.setattr("app.providers.mobile.httpx.AsyncClient", FakeClient)
    body = asyncio.run(adapter._post("https://example.invalid/route", {}, absolute=True))
    assert body["message"] == "请求成功"


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


def test_pansou_parsing_includes_magnet(monkeypatch) -> None:
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test"
    FakeClient.response = {"data": {"merged_by_type": {"magnet": [{"title": "作品 H264", "url": magnet}]}}}
    monkeypatch.setattr(integrations.httpx, "AsyncClient", FakeClient)
    items = asyncio.run(integrations.search_pansou("http://pansou", "作品"))
    assert len(items) == 1
    assert items[0]["provider"] == "magnet"
    assert items[0]["url"] == magnet


def test_magnet_hash_and_torrent_file_list(tmp_path: Path) -> None:
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test"
    assert magnet_info_hash(magnet) == "0123456789abcdef0123456789abcdef01234567"
    torrent = b"d4:infod5:filesld6:lengthi3e4:pathl6:01.mp4eee4:name4:Testee"
    path = tmp_path / "test.torrent"
    path.write_bytes(torrent)
    title, files = torrent_files(path)
    assert title == "Test"
    assert files[0].name == "01.mp4"
    assert files[0].relative_path == "Test/01.mp4"
    assert bdecode(b"i42e") == 42


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


def test_tmdb_details_contains_year_country_genre_and_cast(monkeypatch) -> None:
    FakeClient.response = {
        "id": 100,
        "title": "测试电影",
        "release_date": "2026-08-10",
        "overview": "测试简介",
        "production_countries": [{"name": "中国大陆"}],
        "genres": [{"name": "剧情"}],
        "credits": {"cast": [{"name": "演员甲"}, {"name": "演员乙"}]},
    }
    monkeypatch.setattr(integrations.httpx, "AsyncClient", FakeClient)
    details = asyncio.run(tmdb_details("key", "movie", 100))
    assert details["year"] == "2026"
    assert details["countries"] == ["中国大陆"]
    assert details["genres"] == ["剧情"]
    assert details["cast"] == ["演员甲", "演员乙"]


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
    assert health["version"] == "1.0.12"
    assert health["port_policy"] == "single-port"
    assert health["magnet_playback"] is True
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
    assert 'id="episodeList"' in html


def test_compose_exposes_only_product_port() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"12366:12366"' in compose
    assert "5244" not in compose
    assert compose.count(":12366") == 1
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "aria2" in dockerfile
    assert "ffmpeg" in dockerfile


def test_frontend_has_only_one_delegated_click_handler() -> None:
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert script.count("document.addEventListener('click'") == 1
    assert 'data-intro-search="${esc(media.title||item.title)}"' in script
    assert "document.execCommand('copy')" in script
    assert "data-episode-file" in script
    assert "selected_file_ids:[]" in script
    assert "保存原始分享的全部内容" in script
    assert "inspect-magnet" in script
    assert "/api/magnet/prepare" in script
    assert "115仅支持复制与同盘保存" in script


def test_115_qr_login_uses_alipay_mini_device_type() -> None:
    assert PAN115_QR_APP == "alipaymini"
    assert "/alipaymini/1.0/token/" in PAN115_QR_TOKEN_URL
    assert "/alipaymini/1.0/qrcode?" in pan115_qr_image_url("test-uid")
    assert "/app/1.0/alipaymini/1.0/login/qrcode/" in pan115_qr_login_url()


def test_mobile_layout_has_compact_navigation_and_filters() -> None:
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "@media(max-width:600px)" in css
    assert "grid-template-columns:repeat(auto-fit,minmax(92px,1fr))" in css
    assert ".resource-filters strong{flex:0 0 100%" in css
    assert ".resource-grid .resource-actions{grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert ".resource-grid .resource-card:hover{box-shadow:" in css
    assert ".resource-grid .resource-card:hover{transform:" not in css
