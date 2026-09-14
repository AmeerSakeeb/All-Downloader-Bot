import pytest

from core.exceptions import ExactFormatUnavailableError
from core.models import VideoCodec
from extractors.direct_extractor import DirectMediaExtractor
from jobqueue.scheduler import JobScheduler
from services.output_identity import (
    build_completed_output_identity,
    build_output_identity,
)
from storage.file_manager import FileManager


def test_recovery_finds_only_same_stem_regular_result(tmp_path):
    manager = FileManager(tmp_path / "jobs")
    expected = manager.get_job_file_path("job", "video_stream.mp4")
    actual = expected.with_suffix(".webm")
    actual.write_bytes(b"video")
    manager.get_job_file_path("job", "unrelated.mp4").write_bytes(b"other")

    assert manager.find_job_file("job", expected.name) == actual


@pytest.mark.asyncio
async def test_completed_cache_identity_binds_actual_container(
    db, media_session, video_format, audio_format
):
    execution = build_output_identity(
        media_session, video_format, audio_format, "document"
    )
    mp4_identity = build_completed_output_identity(execution, "mp4")
    mkv_identity = build_completed_output_identity(execution, "mkv")
    assert mp4_identity != mkv_identity

    await db.save_cached_file(
        mkv_identity,
        "telegram-file",
        "document",
        "mkv",
        execution_identity=execution,
    )
    cached = await db.get_cached_file_by_execution_identity(execution)
    assert cached["output_identity"] == mkv_identity
    assert cached["output_container"] == "mkv"

    h265 = video_format.model_copy(
        update={"vcodec_raw": "hev1", "vcodec_normalized": VideoCodec.H265}
    )
    alternate_audio = audio_format.model_copy(update={"format_id": "251"})
    assert build_output_identity(media_session, h265, audio_format, "document") != execution
    assert build_output_identity(
        media_session, video_format, alternate_audio, "document"
    ) != execution


def test_direct_source_identity_ignores_query_tokens_but_rejects_change(video_format):
    headers = {
        "Content-Length": "100",
        "ETag": '"stable-etag"',
        "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
    }
    first = DirectMediaExtractor._source_identity(
        "https://cdn.example/media/video.mp4?token=one", headers, 100
    )
    second = DirectMediaExtractor._source_identity(
        "https://cdn.example/media/video.mp4?token=two", headers, 100
    )
    assert first == second
    assert "token" not in str(first)

    expected = video_format.model_copy(update={"source_identity": first})
    changed = video_format.model_copy(
        update={
            "source_identity": DirectMediaExtractor._source_identity(
                "https://cdn.example/media/video.mp4?token=three",
                {**headers, "ETag": '"different-etag"'},
                100,
            )
        }
    )
    with pytest.raises(ExactFormatUnavailableError):
        JobScheduler._assert_material_identity(expected, changed)
