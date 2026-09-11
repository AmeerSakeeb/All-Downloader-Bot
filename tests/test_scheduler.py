import asyncio

import pytest

from core.exceptions import ExactFormatUnavailableError
from core.models import JobStatus
from jobqueue.manager import QueueManager
from jobqueue.scheduler import JobScheduler
from storage.file_manager import FileManager


class Lease:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class Governor:
    def __init__(self):
        self.deny = set()
        self.released = []
        self.capacity = 2

    async def scheduler_capacity(self, running_jobs):
        return max(running_jobs, self.capacity)

    async def acquire_stage(self, stage):
        return (None, "pressure") if stage in self.deny else (Lease(), "Ready")

    async def reserve_job_disk(self, job_id, requested):
        return True

    def disk_safety_bytes(self):
        return 0


class Downloader:
    def __init__(self, manager):
        self.manager = manager
        self.names = []

    async def download_format(
        self, job_id, url, format_id, output_filename, expected_size, progress, pid, stage,
        **kwargs,
    ):
        self.names.append(output_filename)
        path = self.manager.get_job_file_path(job_id, output_filename)
        path.write_bytes(b"stream")
        await progress(100, len(b"stream"), expected_size, 1, 0)
        return path


class FFmpeg:
    async def merge_streams(self, video, audio, output, **kwargs):
        result = output.with_suffix(".mkv")
        result.write_bytes(video.read_bytes() + audio.read_bytes())
        return result


async def make_scheduler(db, settings, media_session, video_format, audio_format):
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session,
        video_format=video_format,
        audio_format=audio_format,
        chat_id=10,
        message_id=20,
    )
    manager = FileManager(settings.jobs_dir)
    governor = Governor()
    downloader = Downloader(manager)
    queue = QueueManager(db, manager)
    uploads = []

    async def upload(path, current):
        uploads.append(path)
        current.telegram_file_id = "real-file-id"

    scheduler = JobScheduler(
        db, manager, governor, queue, downloader, FFmpeg(), settings, upload_handler=upload
    )
    return scheduler, governor, downloader, job, uploads


@pytest.mark.asyncio
async def test_steel_thread_completes_and_preserves_file_id(
    db, settings, media_session, video_format, audio_format
):
    scheduler, governor, downloader, job, uploads = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    assert await db.claim_job(job.job_id)
    await scheduler.execute_job(job.job_id)
    completed = await db.get_job(job.job_id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.telegram_file_id == "real-file-id"
    assert uploads and downloader.names == ["video_stream.mp4", "audio_stream.m4a"]
    assert not any("137/unsafe-id" in name for name in downloader.names)


@pytest.mark.asyncio
async def test_waiting_resources_then_resume(
    db, settings, media_session, video_format, audio_format
):
    scheduler, governor, _, job, _ = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    governor.deny.add("download")
    assert await db.claim_job(job.job_id)
    await scheduler.execute_job(job.job_id)
    assert (await db.get_job(job.job_id)).status == JobStatus.WAITING_RESOURCES
    governor.deny.clear()
    assert await db.claim_job(job.job_id)
    await scheduler.execute_job(job.job_id)
    assert (await db.get_job(job.job_id)).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_merge_denial_does_not_start_merge(
    db, settings, media_session, video_format, audio_format
):
    scheduler, governor, _, job, uploads = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    governor.deny.add("merge")
    assert await db.claim_job(job.job_id)
    await scheduler.execute_job(job.job_id)
    assert (await db.get_job(job.job_id)).status == JobStatus.WAITING_RESOURCES
    assert uploads == []


@pytest.mark.asyncio
async def test_scheduler_wake_and_dispatch(
    db, settings, media_session, video_format, audio_format
):
    scheduler, _, _, job, _ = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    scheduler._accepting = True
    assert await scheduler.dispatch_once() == 1
    assert job.job_id in scheduler.running_job_ids
    await scheduler.stop()


@pytest.mark.asyncio
async def test_restart_reconciliation_requeues(
    db, settings, media_session, video_format, audio_format
):
    scheduler, _, _, job, _ = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    job.status = JobStatus.DOWNLOADING_VIDEO
    job.process_pid = 999
    await db.save_job(job)
    await scheduler.queue_mgr.reconcile_on_startup()
    recovered = await db.get_job(job.job_id)
    assert recovered.status == JobStatus.QUEUED and recovered.process_pid is None


@pytest.mark.asyncio
async def test_owned_cancellation_cleans_and_releases(
    db, settings, media_session, video_format, audio_format
):
    scheduler, _, _, job, _ = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )
    scheduler.file_mgr.get_job_dir(job.job_id)
    blocker = asyncio.create_task(asyncio.Event().wait())
    scheduler._running[job.job_id] = blocker
    assert await scheduler.cancel_running_job(job.job_id, user_id=1)
    assert blocker.done()
    assert (await db.get_job(job.job_id)).status == JobStatus.CANCELLED
    assert not (settings.jobs_dir / job.job_id).exists()


@pytest.mark.asyncio
async def test_reextraction_is_job_owned_and_missing_exact_format_is_categorized(
    db, settings, media_session, video_format, audio_format
):
    scheduler, _, _, job, _ = await make_scheduler(
        db, settings, media_session, video_format, audio_format
    )

    class Extractor:
        async def extract(self, url, user_id, *, operation_id=None):
            self.operation_id = operation_id
            return media_session.model_copy(update={"formats": []})

    class Registry:
        def __init__(self):
            self.extractor = Extractor()

        async def get_extractor_for_url(self, url):
            return self.extractor

    registry = Registry()
    scheduler.extractor_registry = registry
    assert await db.claim_job(job.job_id)
    await scheduler.execute_job(job.job_id)
    failed = await db.get_job(job.job_id)
    assert registry.extractor.operation_id == job.job_id
    assert failed.status == JobStatus.FAILED
    assert failed.error_category == ExactFormatUnavailableError.error_category
    assert "analyze the link again" in failed.error_message
