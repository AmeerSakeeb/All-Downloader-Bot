from core.models import AudioCodec, MediaFormat, MediaItem, MediaSession, VideoCodec
from extractors.codec_normalizer import normalize_audio_codec, normalize_video_codec
from extractors.format_manager import (
    calculate_total_download_size,
    normalize_format_inventory,
    select_default_audio,
)
from ui.builders import build_collection_text, build_media_info_text, build_format_details_text


def test_h264_aliases():
    assert normalize_video_codec("avc1.640028")[0] == VideoCodec.H264


def test_h265_aliases():
    assert normalize_video_codec("hvc1.1.6.L93")[0] == VideoCodec.H265


def test_vp9_aliases():
    assert normalize_video_codec("vp09.00.51.08")[0] == VideoCodec.VP9


def test_av1_aliases():
    assert normalize_video_codec("av01.0.08M.08")[0] == VideoCodec.AV1


def test_mp4v_is_not_h264():
    normalized, raw = normalize_video_codec("mp4v.20.9")
    assert normalized == VideoCodec.OTHER and raw == "mp4v.20.9"


def test_unknown_codec_preserves_raw():
    normalized, raw = normalize_video_codec("future-codec")
    assert normalized == VideoCodec.OTHER and raw == "future-codec"


def test_audio_normalization():
    assert normalize_audio_codec("mp4a.40.2")[0] == AudioCodec.AAC
    assert normalize_audio_codec("opus")[0] == AudioCodec.OPUS


def test_complete_format_inventory():
    raw = [
        {"format_id": "v", "vcodec": "h264", "acodec": "none", "height": 720},
        {"format_id": "a", "vcodec": "none", "acodec": "opus", "language": "en"},
        {"format_id": "m", "vcodec": "vp9", "acodec": "opus"},
    ]
    assert [item.format_id for item in normalize_format_inventory(raw)] == ["v", "a", "m"]


def test_exact_filesize_display(video_format):
    assert video_format.is_exact_size
    assert not video_format.format_size_display().startswith("~")


def test_approximate_filesize_display():
    item = MediaFormat(format_id="x", filesize_approx=1024**2)
    assert item.format_size_display().startswith("~")


def test_unknown_filesize_display():
    assert MediaFormat(format_id="x").format_size_display() == "Unknown size"


def test_fractional_duration_is_preserved_and_rendered_as_whole_seconds():
    session = MediaSession(
        user_id=1, url="https://example.com/video", duration=33.505,
    )
    assert session.duration == 33.505
    assert "Duration: 00:34" in build_media_info_text(session)


def test_missing_size_is_estimated_from_duration_and_stream_bitrate():
    item = normalize_format_inventory([{
        "format_id": "v", "vcodec": "vp9", "acodec": "none", "tbr": 800,
    }], duration=100)[0]
    assert item.filesize is None
    assert item.filesize_approx == 10_000_000
    assert item.format_size_display().startswith("~")


def test_exact_size_wins_over_possible_calculated_estimate():
    item = normalize_format_inventory([{
        "format_id": "v", "vcodec": "vp9", "acodec": "none", "tbr": 800,
        "filesize": 1234,
    }], duration=100)[0]
    assert item.filesize == 1234
    assert item.filesize_approx is None
    assert not item.format_size_display().startswith("~")


def test_insufficient_estimation_metadata_stays_unknown():
    item = normalize_format_inventory([{
        "format_id": "v", "vcodec": "vp9", "acodec": "none",
    }], duration=100)[0]
    assert item.filesize is None and item.filesize_approx is None
    assert item.format_size_display() == "Unknown size"


def test_format_details_show_estimated_combined_download():
    video, audio = normalize_format_inventory([
        {"format_id": "v", "vcodec": "vp9", "acodec": "none", "vbr": 800},
        {"format_id": "a", "vcodec": "none", "acodec": "opus", "abr": 200},
    ], duration=100)
    text = build_format_details_text(video, audio)
    assert "Video: ~9.5 MB" in text
    assert "Audio: ~2.4 MB" in text
    assert "Total: ~11.9 MB" in text


def test_batch_failures_use_safe_specific_categories():
    session = MediaSession(
        user_id=1, url="https://example.com", session_kind="batch",
        items=[
            MediaItem(source_url="https://facebook.com/x", title="Facebook", analysis_status="failed", analysis_error_category="unexpected_error"),
            MediaItem(source_url="https://tiktok.com/x", title="TikTok", analysis_status="failed", analysis_error_category="site_access_challenge"),
            MediaItem(source_url="https://youtube.com/x", title="YouTube", analysis_status="failed", analysis_error_category="authentication_required"),
        ],
    )
    text = build_collection_text(session)
    assert "Analysis error" in text
    assert "🛡 <b>TikTok</b>" in text
    assert "Site blocked this server" in text
    assert "🔐 <b>YouTube</b>" in text
    assert "Sign-in required" in text


def test_companion_audio_prefers_english(video_format, audio_format):
    other = audio_format.model_copy(update={"internal_key": "other", "audio_language": "fr", "abr": 500})
    assert select_default_audio([other, audio_format], video_format) == audio_format


def test_companion_audio_avoids_commentary(video_format, audio_format):
    commentary = audio_format.model_copy(
        update={"internal_key": "commentary", "format_note": "English commentary", "abr": 999}
    )
    assert select_default_audio([commentary, audio_format], video_format) == audio_format


def test_muxed_size_does_not_add_audio(video_format, audio_format):
    muxed = video_format.model_copy(update={"is_muxed": True, "is_audio": True})
    assert calculate_total_download_size(muxed, audio_format) == (1000, True)


def test_unknown_combined_size(video_format, audio_format):
    unknown = video_format.model_copy(update={"filesize": None})
    assert calculate_total_download_size(unknown, audio_format) == (None, False)


def test_html_presentation_escapes_dynamic_text(media_session):
    text = build_media_info_text(media_session)
    assert "A &lt;title&gt; &amp; symbols" in text
