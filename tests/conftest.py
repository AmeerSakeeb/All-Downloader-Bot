from pathlib import Path

import pytest

from core.config import ResourceMode, Settings
from core.models import AudioCodec, MediaFormat, MediaSession, VideoCodec
from storage.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        admin_user_ids=[1],
        resource_mode=ResourceMode.MANUAL,
        max_concurrent_extractions=2,
        max_concurrent_downloads=2,
        max_concurrent_merges=1,
        max_concurrent_uploads=2,
        data_dir=tmp_path / "data",
        jobs_dir=tmp_path / "data" / "jobs",
        database_path=tmp_path / "data" / "bot.db",
        log_dir=tmp_path / "logs",
        scheduler_retry_seconds=1,
    )


@pytest.fixture
async def db(settings: Settings):
    database = Database(settings.database_path)
    await database.connect()
    await database.bootstrap_admins(settings.admin_user_ids)
    yield database
    await database.close()


@pytest.fixture
def video_format() -> MediaFormat:
    return MediaFormat(
        format_id="137/unsafe-id",
        is_video=True,
        is_audio=False,
        is_muxed=False,
        requires_separate_audio=True,
        vcodec_raw="avc1.640028",
        vcodec_normalized=VideoCodec.H264,
        acodec_normalized=AudioCodec.NONE,
        width=1920,
        height=1080,
        resolution_label="1080p",
        fps=30,
        ext="mp4",
        filesize=1_000,
    )


@pytest.fixture
def audio_format() -> MediaFormat:
    return MediaFormat(
        format_id="140",
        is_video=False,
        is_audio=True,
        is_muxed=False,
        vcodec_normalized=VideoCodec.NONE,
        acodec_raw="mp4a.40.2",
        acodec_normalized=AudioCodec.AAC,
        ext="m4a",
        filesize=100,
        audio_language="en",
    )


@pytest.fixture
def media_session(video_format, audio_format, settings) -> MediaSession:
    return MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl,
        user_id=1,
        url="https://example.com/watch?v=abc",
        canonical_url="https://example.com/watch?v=abc",
        extractor="test",
        title="A <title> & symbols",
        formats=[video_format, audio_format],
    )
