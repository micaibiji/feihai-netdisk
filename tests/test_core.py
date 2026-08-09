from pathlib import Path
import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import (SESSION_COOKIE, SESSION_MAX_AGE, app, create_session_token, settings,
                      validate_session_token)
from app.models import ProviderName
from app.provider_auth import parse_115_qr_state
from app.providers import ProviderRegistry
from app.services import (RANKING_PAGE_SIZE, _discover_tmdb_media, _find_urls,
                          _find_contexts, create_media_bundle, decode_pansou_json, generate_strm,
                          media_folder, media_relative_path, parse_episode, safe_name,
                          trending_tmdb)
from app.storage import JobStore
from app.validation import should_show_resource, validate_share_urls
from app.vault import CredentialVault


def test_detect_supported_providers():
    assert ProviderRegistry.detect("https://pan.baidu.com/s/abc").name == ProviderName.BAIDU
    assert ProviderRegistry.detect("https://pan.quark.cn/s/abc").name == ProviderName.QUARK
    assert ProviderRegistry.detect("https://115.com/s/abc").name == ProviderName.PAN115
    assert ProviderRegistry.detect("https://yun.139.com/share/abc").name == ProviderName.CHINA_MOBILE


def test_provider_display_order():
    assert [item["name"] for item in ProviderRegistry.states()] == [
        "115",
        "baidu",
        "quark",
        "china_mobile",
    ]


def test_safe_media_folder():
    assert safe_name('电影:测试?') == "电影_测试_"
    assert media_folder("流浪地球", "movie", 2019) == "电影/流浪地球 (2019)"


def test_generate_strm(tmp_path: Path):
    settings = Settings(
        app_name="飞海网盘",
        admin_username="admin",
        admin_password="secret",
        data_dir=tmp_path / "data",
        strm_dir=tmp_path / "strm",
        tmdb_api_key="",
        telegram_bot_token="",
        telegram_chat_id="",
        wecom_webhook_url="",
        pansou_base_url="http://pansou:8888",
        provider_priority=("115", "baidu", "quark", "china_mobile"),
        subscription_interval_seconds=1800,
    )
    target = generate_strm(settings, "电影/测试", "测试电影", "https://example.com/play")
    assert target.read_text(encoding="utf-8") == "https://example.com/play\n"


def test_find_urls_in_search_payload():
    payload = {
        "results": [
            {"content": "115资源 https://115.com/s/abc"},
            {"links": ["https://pan.baidu.com/s/xyz"]},
        ]
    }
    assert _find_urls(payload) == ["https://115.com/s/abc", "https://pan.baidu.com/s/xyz"]


def test_nested_pansou_payload_uses_resource_context_instead_of_success():
    payload = {
        "success": True,
        "results": [{
            "channel": "netdisk",
            "content": "汪汪队立大功大电影3 4K https://pan.baidu.com/s/example",
        }],
    }
    contexts = _find_contexts(payload, "汪汪队立大功大电影3")
    assert contexts == [(
        "https://pan.baidu.com/s/example",
        "汪汪队立大功大电影3 4K",
        "netdisk",
    )]


def test_pansou_json_tolerates_one_malformed_plugin_title():
    result = decode_pansou_json(b'{"results":[{"content":"ok\xe6\xaf"}]}')
    assert result["results"][0]["content"].startswith("ok")


def test_external_checker_maps_valid_invalid_and_pending(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "valid_links": ["https://115.com/s/ok"],
                "invalid_links": ["https://pan.baidu.com/s/gone"],
                "pending_links": ["https://pan.quark.cn/s/wait"],
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json, headers):
            assert url == "http://checker.local/api/v1/links/check"
            assert json["selectedPlatforms"] == ["pan115", "baidu", "quark"]
            assert headers == {"Authorization": "Bearer secret"}
            return Response()

    monkeypatch.setattr("app.validation.httpx.AsyncClient", Client)
    urls = [
        "https://115.com/s/ok",
        "https://pan.baidu.com/s/gone",
        "https://pan.quark.cn/s/wait",
    ]
    result = asyncio.run(validate_share_urls(
        urls, base_url="http://checker.local", token="secret",
    ))
    assert [result[url].state for url in urls] == ["valid", "invalid", "unverifiable"]


def test_only_explicitly_invalid_checker_results_are_hidden():
    assert should_show_resource("valid") is True
    assert should_show_resource("unverifiable") is True
    assert should_show_resource("detector_unavailable") is True
    assert should_show_resource("invalid") is False


def test_unified_naming_rules():
    assert media_relative_path("流浪地球", "movie", 2019) == "电影/流浪地球 (2019)/流浪地球 (2019).strm"
    assert media_relative_path("庆余年", "tv", 2019, 1, 3) == "电视剧/庆余年 (2019)/Season 01/庆余年 (2019) - S01E03.strm"
    assert parse_episode("庆余年 S02E36 4K") == (2, 36)
    assert parse_episode("更新至20集") == (1, 20)


def test_media_bundle_has_strm_and_nfo(tmp_path: Path):
    settings = Settings(
        app_name="飞海网盘", admin_username="admin", admin_password="secret",
        data_dir=tmp_path / "data", strm_dir=tmp_path / "strm", tmdb_api_key="",
        telegram_bot_token="", telegram_chat_id="", wecom_webhook_url="",
        pansou_base_url="http://pansou:8888", provider_priority=("115", "baidu", "quark", "china_mobile"),
        subscription_interval_seconds=1800,
    )
    files = create_media_bundle(settings, title="庆余年", media_type="tv", year=2019, season=1, episode=1, play_url="https://example.com/1")
    assert (settings.strm_dir / files["strm"]).read_text(encoding="utf-8").strip() == "https://example.com/1"
    assert "<episodedetails>" in (settings.strm_dir / files["nfo"]).read_text(encoding="utf-8")


def test_multi_source_selects_latest_then_provider_priority(tmp_path: Path):
    store = JobStore(tmp_path / "feihai.db")
    store.initialize()
    subscription = store.create_subscription("测试剧")
    priority = ("115", "baidu", "quark", "china_mobile")
    store.add_subscription_source(subscription["id"], {"provider": "baidu", "url": "https://pan.baidu.com/s/a", "episode": 10}, priority)
    selected = store.add_subscription_source(subscription["id"], {"provider": "115", "url": "https://115.com/s/a", "episode": 10}, priority)
    assert selected["provider"] == "115"
    selected = store.add_subscription_source(subscription["id"], {"provider": "quark", "url": "https://pan.quark.cn/s/a", "episode": 11}, priority)
    assert selected["provider"] == "quark"


def test_credentials_are_encrypted_at_rest(tmp_path: Path):
    vault = CredentialVault(tmp_path)
    vault.save("115", "secret-cookie-value")
    assert vault.load("115") == "secret-cookie-value"
    assert b"secret-cookie-value" not in (tmp_path / "credentials" / "115.token").read_bytes()


def test_signed_session_token_expires_and_rejects_tampering():
    token = create_session_token(settings.admin_username, now=100)
    assert validate_session_token(token, now=101) == settings.admin_username
    assert validate_session_token(token, now=100 + SESSION_MAX_AGE + 1) is None
    assert validate_session_token(token[:-1] + ("A" if token[-1] != "A" else "B"), now=101) is None


def test_browser_login_page_replaces_basic_auth_prompt():
    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        unauthorized = client.get("/api/providers")
        assert unauthorized.status_code == 401
        assert "www-authenticate" not in unauthorized.headers
        login = client.post("/login", data={"username": settings.admin_username, "password": settings.admin_password}, follow_redirects=False)
        assert login.status_code == 303
        assert login.headers["location"] == "/?verified=1"
        assert SESSION_COOKIE in login.cookies
        assert client.get("/").status_code == 200
        assert client.post("/api/verify-password", json={"password": "wrong-password"}).status_code == 401
        verified = client.post("/api/verify-password", json={"password": settings.admin_password})
        assert verified.status_code == 200
        assert verified.json() == {"verified": True}


def test_tmdb_without_key_returns_no_fake_ranking(tmp_path: Path):
    local_settings = Settings(
        app_name="飞海网盘", admin_username="admin", admin_password="secret",
        data_dir=tmp_path / "data", strm_dir=tmp_path / "strm", tmdb_api_key="",
        telegram_bot_token="", telegram_chat_id="", wecom_webhook_url="",
        pansou_base_url="http://pansou:8888", provider_priority=("115", "baidu", "quark", "china_mobile"),
        subscription_interval_seconds=1800,
    )
    result = asyncio.run(trending_tmdb(local_settings))
    assert result["live"] is False
    assert result["items"] == []
    assert "配置" in result["message"]


def test_tmdb_discover_supports_24_item_logical_pages():
    class Response:
        def __init__(self, page: int):
            self.page = page

        def raise_for_status(self):
            return None

        def json(self):
            first = (self.page - 1) * 20
            return {
                "total_results": 240,
                "results": [
                    {
                        "id": first + index,
                        "title": f"电影 {first + index}",
                        "release_date": f"2026-08-{max(1, 31 - index):02d}",
                        "popularity": index,
                    }
                    for index in range(20)
                ],
            }

    class Client:
        def __init__(self):
            self.pages = []

        async def get(self, _url, params):
            self.pages.append(params["page"])
            assert params["sort_by"] == "primary_release_date.desc"
            assert "primary_release_date.lte" in params
            assert params["vote_count.gte"] == 10
            return Response(params["page"])

    client = Client()
    result = asyncio.run(_discover_tmdb_media(
        client, "movie", api_key="key", language="zh-CN", region="CN",
        page=2, page_size=RANKING_PAGE_SIZE,
    ))
    assert client.pages == [2, 3]
    assert len(result["items"]) == 24
    assert all(item["media_type"] == "movie" for item in result["items"])
    assert result["total_pages"] == 10


def test_home_ranking_is_paginated_and_has_no_date_limit():
    javascript = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "每页 24 部" in javascript
    assert "data-ranking-page" in javascript
    assert "全量内容，不限制日期" in javascript
    assert "tmdb_ranking_window" not in javascript
    assert 'if (!state.overview)' in javascript


def test_resource_visibility_requires_recognition_and_validation(tmp_path: Path):
    local_store = JobStore(tmp_path / "feihai.db")
    local_store.initialize()
    record = local_store.upsert_resource({
        "fingerprint": "abc", "provider": "115", "url": "https://115.com/s/abc",
        "title": "庆余年 S02E36", "normalized_title": "庆余年", "source": "test",
        "season": 2, "episode": 36, "quality": "4K", "recognition_state": "recognized",
    })
    assert local_store.list_resources() == []
    local_store.update_resource_validation(
        record["fingerprint"], state="valid", reason="分享页面可以正常访问",
        checked_at="2026-08-08T00:00:00+00:00", recheck_after="2026-08-08T02:00:00+00:00",
    )
    assert local_store.list_resources()[0]["normalized_title"] == "庆余年"


def test_auth_session_does_not_expose_secret_key(tmp_path: Path):
    local_store = JobStore(tmp_path / "feihai.db")
    local_store.initialize()
    session = local_store.create_auth_session(
        session_id="session1", provider="115", method="qr", state="waiting",
        public_payload={"qr_image_url": "https://example.com/qr"}, secret_key="private-key",
    )
    assert "secret_key" not in session
    assert local_store.get_auth_session("session1", include_secret=True)["secret_key"] == "private-key"


def test_generic_secrets_are_encrypted_at_rest(tmp_path: Path):
    local_vault = CredentialVault(tmp_path)
    local_vault.save_secret("tmdb_api_key", "top-secret")
    assert local_vault.load_secret("tmdb_api_key") == "top-secret"
    assert b"top-secret" not in (tmp_path / "credentials" / "tmdb_api_key.token").read_bytes()


def test_115_empty_long_poll_payload_is_still_waiting():
    assert parse_115_qr_state({"state": 1, "code": 0, "data": {}}) == ("waiting", "等待扫码")
    assert parse_115_qr_state({"data": {"status": "1"}})[0] == "scanned"


def test_settings_hide_internal_gateway_and_link_tmdb_guide():
    javascript = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "OpenList 地址" not in javascript
    assert 'id="openlistForm"' not in javascript
    assert "https://www.themoviedb.org/settings/api" in javascript
    assert "填写教程" in javascript
    assert 'id="pansouForm"' in javascript
    assert 'id="checkerForm"' in javascript
    assert "/api/v1/links/check" in javascript


def test_unavailable_provider_login_has_honest_in_page_guidance():
    javascript = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "授权说明" in javascript
    assert "夸克网页版可以扫码" in javascript
    assert "中国移动开放平台面向申请接入的应用" in javascript
    assert "Token、Cookie 与扫码凭证都只保存在本机" in javascript


def test_all_four_providers_offer_local_encrypted_token_login():
    javascript = Path("app/static/app.js").read_text(encoding="utf-8")
    assert 'data-token-auth="${x.name}"' in javascript
    assert "Token、Cookie 与扫码凭证都只保存在本机" in javascript
    assert "/credential" in javascript


def test_compose_only_publishes_the_feihai_port():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert '"12366:12366"' in compose
    assert '"5244:5244"' not in compose
    assert '"8888:8888"' not in compose
    assert "feihai-pansou" not in compose
    assert "ghcr.io/fish2018/pansou" not in compose
