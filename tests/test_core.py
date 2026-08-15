from __future__ import annotations

import base64
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import integrations
from app.integrations import tmdb_details
from app.magnet import bdecode, magnet_info_hash, torrent_files
from app.providers.baidu import BaiduAdapter
from app.providers.base import AuthenticationError, BrowserSupport, CloudError, DirectLink, FolderEntry, ShareFile, browser_support
from app.providers.mobile import MobileAdapter
from app.providers.pan115 import Pan115Adapter
from app.providers.auth import PAN115_QR_APP, PAN115_QR_TOKEN_URL, pan115_qr_image_url, pan115_qr_login_url
from app.providers.quark import QuarkAdapter
from app.providers.quark_tv import start_quark_tv_qr
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


def test_115_delete_uses_async_safe_form_body(monkeypatch) -> None:
    adapter = Pan115Adapter("UID=1; CID=2")
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return {"state": True}

    monkeypatch.setattr(adapter, "request", fake_request)
    asyncio.run(adapter.delete(["11", "22"]))
    assert captured["content"] == b"fid%5B%5D=11&fid%5B%5D=22"
    assert "data" not in captured


def test_quark_delete_treats_already_deleted_as_success(monkeypatch) -> None:
    adapter = QuarkAdapter("__puus=test")

    async def fake_request(*args, **kwargs):
        raise CloudError("夸克接口 400：[文件已经删除,请稍后重试]")

    monkeypatch.setattr(adapter, "request", fake_request)
    asyncio.run(adapter.delete(["gone"]))


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


def test_quark_tv_device_limit_falls_back_to_browser_cookie(monkeypatch) -> None:
    adapter = QuarkAdapter(json.dumps({
        "cookie": "__puus=secret; kps=token",
        "tv_refresh_token": "refresh",
        "tv_device_id": "device",
    }))

    tv_attempts = 0

    async def rejected_tv_stream(*_args) -> str:
        nonlocal tv_attempts
        tv_attempts += 1
        raise AuthenticationError("夸克电视端授权失败：设备数超限")

    async def fake_request(_method, url, **_kwargs):
        if url.endswith("/file/v2/play/project"):
            raise CloudError("data invalid: [plf_invalid]")
        return {"data": [{"download_url": "https://download.example/video.mp4"}]}

    monkeypatch.setattr("app.providers.quark.quark_tv_stream_link", rejected_tv_stream)
    monkeypatch.setattr(adapter, "request", fake_request)
    direct = asyncio.run(adapter.direct_link(ShareFile(id="f1", name="video.mp4")))
    assert direct.url == "https://download.example/video.mp4"
    assert direct.headers["Cookie"] == "__puus=secret; kps=token"
    assert direct.redirect is False
    second = asyncio.run(adapter.direct_link(ShareFile(id="f2", name="second.mp4")))
    assert second.url == "https://download.example/video.mp4"
    assert tv_attempts == 1


def test_quark_tv_qr_reuses_the_existing_nas_device(monkeypatch) -> None:
    captured = {}

    async def fake_request(method, path, device_id, **_kwargs):
        captured.update(method=method, path=path, device_id=device_id)
        return {"qr_data": "image", "query_token": "query"}

    monkeypatch.setattr("app.providers.quark_tv._request", fake_request)
    public, secret = asyncio.run(start_quark_tv_qr("existing-device-id"))
    assert public["qr_image_url"].endswith("image")
    assert secret == {"device_id": "existing-device-id", "query_token": "query"}
    assert captured["device_id"] == "existing-device-id"


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


def test_failed_temp_cleanup_is_retried_after_six_hours(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now = datetime.now(UTC)
    store.add_temp(
        {
            "id": "cleanup", "provider": "quark", "title": "测试",
            "share_url": "https://pan.quark.cn/s/abc", "cloud_file_id": "f1",
            "file_name": "测试.mp4", "state": "cleanup_failed",
            "direct_hint": {"last_cleanup_attempt_at": now.isoformat()},
            "last_played_at": (now - timedelta(days=3)).isoformat(),
            "expires_at": (now - timedelta(days=1)).isoformat(), "created_at": now.isoformat(),
        }
    )
    assert store.expired_temps(now.isoformat()) == []
    store.update_temp("cleanup", direct_hint={"last_cleanup_attempt_at": (now - timedelta(hours=7)).isoformat()})
    assert [item["id"] for item in store.expired_temps(now.isoformat())] == ["cleanup"]


def test_restart_recovers_interrupted_jobs_and_temporary_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    now = datetime.now(UTC)
    job = store.create_job("permanent_save", "baidu", "测试任务", {"title": "测试任务"})
    store.update_job(job["id"], status="running", progress=45, stage="正在保存")
    base_temp = {
        "title": "测试", "share_url": "https://pan.quark.cn/s/abc",
        "cloud_file_id": "f1", "file_name": "01.mp4",
        "last_played_at": now.isoformat(),
        "expires_at": (now - timedelta(hours=1)).isoformat(),
        "created_at": now.isoformat(),
    }
    store.add_temp({
        **base_temp, "id": "prepare-cloud", "provider": "quark", "state": "preparing",
        "direct_hint": {"progress": 35, "source_file_id": "source"},
    })
    store.add_temp({
        **base_temp, "id": "prepare-magnet", "provider": "magnet", "state": "preparing",
        "direct_hint": {"progress": 20},
    })
    store.add_temp({
        **base_temp, "id": "cleanup", "provider": "quark", "state": "cleanup_pending",
        "direct_hint": {"last_cleanup_attempt_at": now.isoformat(), "path": "/影视临时播放/01.mp4"},
    })

    recovered = store.recover_interrupted()

    interrupted = store.job(job["id"])
    assert interrupted["status"] == "failed"
    assert "重启" in interrupted["error"]
    assert store.temp("prepare-cloud")["state"] == "failed"
    assert "重新点击播放" in store.temp("prepare-cloud")["direct_hint"]["error"]
    assert store.temp("prepare-magnet")["state"] == "failed"
    assert recovered["magnet_temp_ids"] == ["prepare-magnet"]
    cleanup = store.temp("cleanup")
    assert cleanup["state"] == "cleanup_failed"
    assert "last_cleanup_attempt_at" not in cleanup["direct_hint"]
    assert [item["id"] for item in store.expired_temps(now.isoformat())] == ["cleanup"]
    assert recovered == {
        "jobs": 1, "preparing": 2, "cleanup": 1, "magnet_temp_ids": ["prepare-magnet"]
    }


def test_restart_recovery_keeps_finished_work_unchanged(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    job = store.create_job("permanent_save", "quark", "已完成", {})
    store.update_job(job["id"], status="success", progress=100, stage="已完成")
    now = datetime.now(UTC)
    store.add_temp({
        "id": "ready", "provider": "quark", "title": "测试",
        "share_url": "https://pan.quark.cn/s/abc", "cloud_file_id": "f1",
        "file_name": "01.mp4", "state": "ready", "direct_hint": {},
        "last_played_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=48)).isoformat(), "created_at": now.isoformat(),
    })

    recovered = store.recover_interrupted()

    assert recovered == {"jobs": 0, "preparing": 0, "cleanup": 0, "magnet_temp_ids": []}
    assert store.job(job["id"])["status"] == "success"
    assert store.temp("ready")["state"] == "ready"


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
    assert health["version"] == "1.0.27"
    assert health["port_policy"] == "single-port"
    assert health["magnet_playback"] is True
    assert client.get("/api/admin/overview").status_code == 401
    response = client.post(
        "/api/login",
        json={"username": main.settings.admin_username, "password": main.settings.admin_password},
    )
    assert response.status_code == 200
    assert "max-age" not in response.headers["set-cookie"].lower()
    assert client.get("/api/admin/overview").status_code == 200
    assert client.post("/api/admin/integrations/telegram/test").status_code == 409


def test_same_nas_service_urls_use_container_host_gateway(web_client) -> None:
    _, main = web_client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/api/search",
            "raw_path": b"/api/search",
            "query_string": b"",
            "headers": [(b"host", b"192.168.100.213:12366")],
            "client": ("192.168.100.10", 50000),
            "server": ("192.168.100.213", 12366),
        }
    )

    assert main._container_service_url("http://192.168.100.213:8888", request) == "http://host.docker.internal:8888"
    assert main._container_service_url("http://192.168.100.213:12110/api", request) == "http://host.docker.internal:12110/api"
    assert main._container_service_url("http://192.168.100.225:8888", request) == "http://192.168.100.225:8888"


def test_search_checker_timeout_keeps_unverified_resources(web_client, monkeypatch) -> None:
    client, main = web_client

    async def fake_search(*_args):
        return [{
            "provider": "quark", "provider_label": "夸克网盘", "title": "九门",
            "url": "https://pan.quark.cn/s/example", "extraction_code": "", "source": "测试",
            "fingerprint": "test-resource", "season": 0, "episode": 0, "quality": "1080P",
            "recognized": True, "validation_state": "unverifiable", "validation_reason": "尚未检测",
        }]

    async def fake_tmdb(*_args):
        return []

    async def slow_checker(*_args):
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(main, "search_pansou", fake_search)
    monkeypatch.setattr(main, "search_tmdb", fake_tmdb)
    monkeypatch.setattr(main, "check_links", slow_checker)
    monkeypatch.setattr(main, "LINK_CHECK_TIMEOUT_SECONDS", 0.005)
    response = client.get("/api/search", params={"q": "九门"})
    assert response.status_code == 200
    body = response.json()
    assert body["warning"] == "检测网站响应超时，未明确失效的资源已保留显示"
    assert body["resources"][0]["validation_state"] == "unverifiable"


def test_search_pansou_timeout_returns_actionable_error(web_client, monkeypatch) -> None:
    client, main = web_client

    async def slow_search(*_args):
        await asyncio.sleep(0.05)
        return []

    async def fake_tmdb(*_args):
        return []

    monkeypatch.setattr(main, "search_pansou", slow_search)
    monkeypatch.setattr(main, "search_tmdb", fake_tmdb)
    monkeypatch.setattr(main, "PANSOU_SEARCH_TIMEOUT_SECONDS", 0.005)
    response = client.get("/api/search", params={"q": "九门"})
    assert response.status_code == 504
    assert response.json()["detail"] == "PanSou 搜索超时，请检查 PanSou 服务后重试"


def test_index_disables_referer(web_client) -> None:
    client, _ = web_client
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"
    assert '<meta name="referrer" content="no-referrer">' in response.text


def test_cloud_transcode_redirect_omits_referer(web_client, monkeypatch) -> None:
    client, main = web_client
    now = datetime.now(UTC)
    main.store.add_temp(
        {
            "id": "quark-ready",
            "provider": "quark",
            "title": "测试视频",
            "share_url": "https://pan.quark.cn/s/example",
            "cloud_file_id": "f1",
            "file_name": "01.mp4",
            "mime_type": "video/mp4",
            "state": "ready",
            "direct_hint": {},
            "last_played_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=48)).isoformat(),
            "created_at": now.isoformat(),
        }
    )

    class RedirectAdapter:
        async def direct_link(self, _file):
            return DirectLink("https://video.example/01.mp4", {}, "video/mp4", redirect=True)

    monkeypatch.setattr(main, "_adapter", lambda _provider: RedirectAdapter())
    response = client.get("/api/play/quark-ready", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-feihai-playback"] == "cloud-transcode"


def test_playback_reuse_happens_before_rate_limit() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    cloud_prepare = source.split('async def prepare_play', 1)[1].split('def _magnet_temp_item', 1)[0]
    magnet_prepare = source.split('async def prepare_magnet', 1)[1].split('@app.get("/api/magnet/status', 1)[0]
    assert cloud_prepare.index("if existing:") < cloud_prepare.index("_limit_play(request)")
    assert magnet_prepare.index("if existing and") < magnet_prepare.index("_limit_play(request)")


def test_resource_match_scores_exact_title_year_and_type() -> None:
    import app.main as main

    item = {"title": "庆余年 第二季 2024 S02E01 1080P", "recognized": True, "episode": 1, "validation_state": "valid"}
    media = {"title": "庆余年 第二季", "year": "2024", "media_type": "tv"}
    score, reasons = main._resource_match(item, media, "庆余年")
    assert score == 100
    assert {"片名匹配", "年份匹配", "影视类型匹配", "链接已验证"}.issubset(reasons)


def test_cloud_play_status_and_cancel(web_client) -> None:
    client, main = web_client
    now = datetime.now(UTC)
    main.store.add_temp(
        {
            "id": "pending-cloud", "provider": "quark", "title": "测试", "share_url": "https://pan.quark.cn/s/test",
            "cloud_file_id": "source", "file_name": "01.mp4", "state": "preparing",
            "direct_hint": {"progress": 35, "stage": "正在同盘保存所选视频"},
            "last_played_at": now.isoformat(), "expires_at": (now + timedelta(hours=48)).isoformat(), "created_at": now.isoformat(),
        }
    )
    status = client.get("/api/play/status/pending-cloud").json()
    assert status["state"] == "preparing"
    assert status["progress"] == 35
    assert client.delete("/api/play/status/pending-cloud").status_code == 200
    assert main.store.temp("pending-cloud")["state"] == "canceled"


def test_portable_backup_restore_round_trip(tmp_path: Path) -> None:
    first = Store(tmp_path / "first.db")
    first.initialize()
    first.save_settings({"pansou_url": "http://pansou"})
    first.add_subscription("庆余年", "tv", 2024)
    first.save_last_directory("quark", "folder", "/影视")
    payload = first.export_portable()
    second = Store(tmp_path / "second.db")
    second.initialize()
    second.restore_portable(payload)
    assert second.settings()["pansou_url"] == "http://pansou"
    assert second.subscriptions()[0]["title"] == "庆余年"
    assert second.last_directories()["quark"]["folder_path"] == "/影视"


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
    assert 'id="copyDialog"' in html
    assert 'id="toast" class="toast" popover="manual"' in html


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
    assert script.index("navigator.clipboard?.writeText") < script.index("legacyCopyText(value);return false")
    assert "data-job-retry" in script
    assert "data-temp-cleanup" in script
    assert "百度分享缺少转存信息" in script
    assert "格式检查完成" in script
    assert "data-episode-file" in script
    assert "selected_file_ids:[]" in script
    assert "保存原始分享的全部内容" in script
    assert "inspect-magnet" in script
    assert "/api/magnet/prepare" in script
    assert "115 不提供网页播放" in script
    assert "可播放；片名可能不一致，请自行确认" in script
    assert "可播放；集数形态可能不一致，请自行确认" in script
    assert "data-resource-view" in Path("app/static/index.html").read_text(encoding="utf-8")
    assert "localStorage.getItem('feihai-resource-view')" in script
    assert "resource-list-view" in Path("app/static/app.css").read_text(encoding="utf-8")
    assert "showToastLayer(el)" in script
    assert "function showDialog(dialog)" in script
    assert "timeoutMs:38000" in script
    assert "data-retry-search" in script
    assert "z-index:2147483647" in Path("app/static/app.css").read_text(encoding="utf-8")


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
    assert ".player-layout{grid-template-columns:1fr;grid-template-rows:auto auto;height:auto" in css
