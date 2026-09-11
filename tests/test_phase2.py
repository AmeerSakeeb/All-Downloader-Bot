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
