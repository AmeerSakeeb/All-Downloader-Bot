from pathlib import Path

import pytest

from core.models import FavoriteFormatRule, MatchingStrategy, VideoCodec
from downloads.downloader import Downloader
from services.favorites import FavoriteMatcher


def test_favorite_combinations_are_exact_and_any_is_exclusive(
    video_format, audio_format
):
    rule = FavoriteFormatRule(
        user_id=1,
        codecs=["any", "h265"],
        resolutions=["1080", "720"],
        fps_values=["best"],
        containers=["mp4"],
    )
    assert rule.codecs == ["any"]
    exact_h265 = video_format.model_copy(
        update={"vcodec_raw": "hev1", "vcodec_normalized": VideoCodec.H265}
    )
    strict = rule.model_copy(update={"codecs": ["h265"]})
    result = FavoriteMatcher.match(
        [exact_h265, video_format, audio_format], [strict], MatchingStrategy.BEST_QUALITY
    )
    assert [fmt.internal_key for fmt in result.formats] == [exact_h265.internal_key]
    assert result.total_combinations == 2 and result.unavailable_combinations == 1


@pytest.mark.asyncio
async def test_phase2_settings_rules_and_cache_round_trip(db):
    settings = await db.get_user_settings(1)
    settings = settings.model_copy(update={"send_mode": "video"})
    await db.save_user_settings(settings)
    rule = FavoriteFormatRule(user_id=1, codecs=["h265", "av1"], priority=0)
    await db.save_favorite_rule(rule)
    await db.save_cached_file("identity", "telegram-file", "video", "mp4")
    assert (await db.get_user_settings(1)).send_mode == "video"
    assert (await db.list_favorite_rules(1))[0].codecs == ["h265", "av1"]
    assert (await db.get_cached_file("identity"))["telegram_file_id"] == "telegram-file"


def test_direct_source_command_never_uses_synthetic_format_selector(settings):
    downloader = Downloader.__new__(Downloader)
    downloader.settings = settings
    downloader.proxy_url = "http://127.0.0.1:1234"
    command = downloader._build_ytdlp_command(
        "https://example.com/video.mp4", "direct", Path("video.mp4"), True
    )
    assert "-f" not in command and "direct" not in command
    assert "--proxy" in command and "--continue" in command


def test_cookie_domains_config_parsing_and_validation(tmp_path):
    from core.config import Settings
    s = Settings(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        ytdlp_cookie_domains=" YouTube.com. , vimeo.com. ",
    )
    assert s.ytdlp_cookie_domains == ["youtube.com", "vimeo.com"]

    with pytest.raises(ValueError):
        Settings(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            ytdlp_cookie_domains="invalid_domain_name",
        )


def test_cookies_file_without_domains_raises(tmp_path):
    from core.config import Settings
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    with pytest.raises(ValueError, match="YTDLP_COOKIE_DOMAINS is empty"):
        Settings(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            ytdlp_cookies_file=cookie_file,
            ytdlp_cookie_domains="",
        )


def test_cookie_profile_source_and_cookie_domains_separation(tmp_path):
    from services.cookie_profiles import CookieProfile
    cookie_file = tmp_path / "cookies.txt"
    profile = CookieProfile(
        name="test",
        source_domains=("youtube.com", "youtu.be"),
        path=cookie_file,
        cookie_domains=("youtube.com", ".google.com"),
    )
    assert profile.source_domains == ("youtube.com", "youtu.be")
    assert profile.domains == ("youtube.com", "youtu.be")
    assert profile.cookie_domains == ("youtube.com", ".google.com")
    assert profile.matches("https://youtu.be/abc")
    assert not profile.matches("https://google.com/abc")
