from pathlib import Path

from app.config import Settings
from app.models import ProviderName
from app.providers import ProviderRegistry
from app.services import _find_urls, generate_strm, media_folder, safe_name


def test_detect_supported_providers():
    assert ProviderRegistry.detect("https://pan.baidu.com/s/abc").name == ProviderName.BAIDU
    assert ProviderRegistry.detect("https://pan.quark.cn/s/abc").name == ProviderName.QUARK
    assert ProviderRegistry.detect("https://115.com/s/abc").name == ProviderName.PAN115
    assert ProviderRegistry.detect("https://yun.139.com/share/abc").name == ProviderName.CHINA_MOBILE


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
