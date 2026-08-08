from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import (SESSION_COOKIE, SESSION_MAX_AGE, app, create_session_token, settings,
                      validate_session_token)
from app.models import ProviderName
from app.providers import ProviderRegistry
from app.services import (_find_urls, create_media_bundle, generate_strm,
                          media_folder, media_relative_path, parse_episode, safe_name)
from app.storage import JobStore
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
