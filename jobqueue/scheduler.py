"""Bounded persistent scheduler for the Phase 1 download steel thread."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.config import ResourceMode, Settings, get_settings
from core.exceptions import BotError, ExactFormatUnavailableError, ResourceExhaustedError
from core.models import DownloadJob, JobStatus, MediaFormat, MediaSession
from downloads.downloader import Downloader
from downloads.ffmpeg_manager import FFmpegManager
from extractors.format_manager import calculate_total_download_size
from extractors.interface import Extractor
from extractors.registry import ExtractorRegistry
from services.telegram_api import DeliverySizeError, TelegramService
from services.errors import safe_job_error
from jobqueue.manager import QueueManager
from resources.governor import ResourceGovernor
from storage.database import Database
from storage.file_manager import FileManager, safe_extension

ProgressHandler = Callable[[DownloadJob], Awaitable[None] | None]
UploadHandler = Callable[[Path, DownloadJob], Awaitable[None]]
ExtractorFactory = Callable[[str], Awaitable[Extractor]]


class _ExtractionResourcesUnavailable(ResourceExhaustedError):
    """Admission denial, distinct from execution-time disk/resource failures."""


class JobScheduler:
    def __init__(
        self,
        db: Database,
        file_mgr: FileManager,
        governor: ResourceGovernor,
        queue_mgr: QueueManager,
        downloader: Downloader,
        ffmpeg_mgr: FFmpegManager,
        settings: Optional[Settings] = None,
        *,
        upload_handler: Optional[UploadHandler] = None,
        progress_handler: Optional[ProgressHandler] = None,
        extractor_factory: Optional[ExtractorFactory] = None,
        extractor_registry: Optional[ExtractorRegistry] = None,
        telegram_service: Optional[TelegramService] = None,
    ):
        self.db = db
        self.file_mgr = file_mgr
        self.governor = governor
        self.queue_mgr = queue_mgr
        self.downloader = downloader
        self.ffmpeg_mgr = ffmpeg_mgr
        self.settings = settings or get_settings()
        self.upload_handler = upload_handler
        self.progress_handler = progress_handler
        self.extractor_factory = extractor_factory
        self.extractor_registry = extractor_registry
        self.telegram_service = telegram_service
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._service_task: Optional[asyncio.Task[None]] = None
        self._running: dict[str, asyncio.Task[None]] = {}
        self._accepting = False
        self._paused = False

    @property
    def running_job_ids(self) -> set[str]:
        return set(self._running)

    @property
    def is_alive(self) -> bool:
        return bool(self._accepting and self._service_task and not self._service_task.done())

    @property
    def paused(self) -> bool:
        return self._paused

    def pause_new_jobs(self) -> None:
        self._paused = True

    def resume_new_jobs(self) -> None:
        self._paused = False
        self.wake()

    async def start(self) -> None:
        if self._service_task and not self._service_task.done():
            return
        self._accepting = True
        self._stop_event.clear()
        self._service_task = asyncio.create_task(self.run(), name="job-scheduler")
        self.wake()

    def wake(self) -> None:
        self._wake_event.set()

    async def cancel_running_job(self, job_id: str, user_id: int | None = None) -> bool:
        """Persist cancellation, terminate owned processes, then reap the job task."""
        job = await self.db.get_job(job_id)
        if not job or (user_id is not None and job.user_id != user_id):
            return False
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job.status == JobStatus.CANCELLED
        job.status = JobStatus.CANCELLED
        job.current_stage = "Cancelled"
        job.claimed_at = None
        await self.db.save_job(job)
        await self.queue_mgr.supervisor.cancel_job(job_id)
        task = self._running.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._running.pop(job_id, None)
        await self.db.release_disk_reservation(job_id)
        self.file_mgr.cleanup_job(job_id)
        await self._notify(job)
        self.wake()
        return True

    async def _raise_if_cancelled(self, job_id: str) -> None:
        current = await self.db.get_job(job_id)
        if current and current.status == JobStatus.CANCELLED:
            raise asyncio.CancelledError

    async def stop(self) -> None:
        self._accepting = False
        self._stop_event.set()
        self._wake_event.set()
        if self._service_task:
            self._service_task.cancel()
            await asyncio.gather(self._service_task, return_exceptions=True)
        if self._running:
            for task in self._running.values():
                task.cancel()
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            self._reap_completed()
            await self.dispatch_once()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self.settings.scheduler_retry_seconds
                )
            except asyncio.TimeoutError:
                continue

    def _reap_completed(self) -> None:
        for job_id, task in list(self._running.items()):
            if task.done():
                self._running.pop(job_id, None)
                try:
                    task.result()
                except (Exception, asyncio.CancelledError):
                    pass

    async def dispatch_once(self) -> int:
        if not self._accepting or self._paused:
            return 0
        self._reap_completed()
        capacity = await self.governor.scheduler_capacity(len(self._running))
        if self.settings.scheduler_max_workers > 0:
            capacity = min(capacity, self.settings.scheduler_max_workers)
        slots = capacity - len(self._running)
        if slots <= 0:
            return 0
        dispatched = 0
        for job in await self.db.list_queued_jobs(limit=slots):
            if job.job_id in self._running or not await self.db.claim_job(job.job_id):
                continue
            task = asyncio.create_task(
                self.execute_job(job.job_id), name=f"download-job-{job.job_id}"
            )
            self._running[job.job_id] = task
            dispatched += 1
        return dispatched

    async def _notify(
        self, job: DownloadJob, handler: Optional[ProgressHandler] = None
    ) -> None:
        callback = handler or self.progress_handler
        if callback:
            result = callback(job)
            if inspect.isawaitable(result):
                await result

    async def _set_status(
        self,
        job: DownloadJob,
        status: JobStatus,
        label: str,
        error: Optional[str] = None,
        progress_handler: Optional[ProgressHandler] = None,
    ) -> None:
        persisted = await self.db.get_job(job.job_id)
        if persisted and persisted.status == JobStatus.CANCELLED and status != JobStatus.CANCELLED:
            raise asyncio.CancelledError
        job.status = status
        job.current_stage = label
        job.error_message = error
        if status != JobStatus.CLAIMED:
            job.claimed_at = None
        await self.db.save_job(job)
        await self._notify(job, progress_handler)

    async def execute_job(
        self,
        job_id: str,
        on_progress_update: Optional[ProgressHandler] = None,
        upload_handler: Optional[UploadHandler] = None,
    ) -> None:
        job = await self.db.get_job(job_id)
        if not job:
            return
        progress = on_progress_update or self.progress_handler
        upload = upload_handler or self.upload_handler
        terminal = False
        try:
            video, audio = await self._execution_formats(job)
            await self._raise_if_cancelled(job_id)
            if self.telegram_service:
                allowed, reason = self.telegram_service.can_deliver_selection(video, audio)
                if not allowed:
                    raise DeliverySizeError(reason, user_message=reason)
            requested_bytes = self._projected_reservation(video, audio, job.job_id)
            lease, reason = await self.governor.acquire_stage("download")
            if not lease:
                await self._set_status(
                    job, JobStatus.WAITING_RESOURCES, f"Waiting for resources: {reason}"
                )
                return
            async with lease:
                if not await self.governor.reserve_job_disk(job.job_id, requested_bytes):
                    await self._set_status(
                        job,
                        JobStatus.WAITING_RESOURCES,
                        "Waiting for protected disk headroom",
                    )
                    return
                await self._raise_if_cancelled(job_id)
                final_path = await self._download(job, video, audio, progress)

            if audio:
                await self._raise_if_cancelled(job_id)
                lease, reason = await self.governor.acquire_stage("merge")
                if not lease:
                    await self._set_status(
                        job, JobStatus.WAITING_RESOURCES, f"Waiting for merge: {reason}"
                    )
                    return
                async with lease:
                    await self._set_status(
                        job, JobStatus.MERGING, "Combining streams losslessly"
                    )
                    video_path = self.file_mgr.get_job_file_path(
                        job.job_id, f"video_stream.{safe_extension(video.ext)}"
                    )
                    audio_path = self.file_mgr.get_job_file_path(
                        job.job_id, f"audio_stream.{safe_extension(audio.ext)}"
                    )
                    final_path = await self.ffmpeg_mgr.merge_streams(
                        video_path,
                        audio_path,
                        self.file_mgr.get_job_file_path(
                            job.job_id, f"merged_output.{safe_extension(video.ext, 'mkv')}"
                        ),
                        job_id=job.job_id,
                    )

            await self._raise_if_cancelled(job_id)
            lease, reason = await self.governor.acquire_stage("upload")
            if not lease:
                await self._set_status(
                    job, JobStatus.WAITING_RESOURCES, f"Waiting for upload: {reason}"
                )
                return
            async with lease:
                await self._set_status(job, JobStatus.UPLOADING, "Uploading")
                job.output_path = str(final_path)
                await self.db.save_job(job)
                if not upload:
                    raise RuntimeError("No Telegram upload handler is configured")
                await upload(final_path, job)
                await self.db.save_job(job)
            await self._raise_if_cancelled(job_id)
            await self._set_status(job, JobStatus.COMPLETED, "Completed")
            terminal = True
        except asyncio.CancelledError:
            current = await self.db.get_job(job_id)
            if current and current.status != JobStatus.CANCELLED:
                current.status = JobStatus.QUEUED
                current.current_stage = "Interrupted; queued for recovery"
                current.process_pid = None
                await self.db.save_job(current)
            raise
        except _ExtractionResourcesUnavailable:
            await self._set_status(job, JobStatus.WAITING_RESOURCES, "Waiting for extraction resources")
        except Exception as error:
            current = await self.db.get_job(job_id)
            if current and current.status != JobStatus.CANCELLED:
                current.error_category = (
                    error.error_category if isinstance(error, BotError) else "unexpected_error"
                )
                # Some low-level BotError messages contain paths/URLs; never
                # forward diagnostics to Telegram or the persisted public message.
                message = safe_job_error(error)
                await self._set_status(current, JobStatus.FAILED, "Failed", message)
                terminal = True
        finally:
            current = await self.db.get_job(job_id)
            if terminal or (current and current.status == JobStatus.CANCELLED):
                await self.db.release_disk_reservation(job_id)
                self.file_mgr.cleanup_job(job_id)

    async def _download(
        self,
        job: DownloadJob,
        video: MediaFormat,
        audio: Optional[MediaFormat],
        progress_handler: Optional[ProgressHandler] = None,
    ) -> Path:
        async def pid_update(pid: int) -> None:
            job.process_pid = pid
            await self.db.save_job(job)

        async def video_progress(pct, downloaded, total, speed, eta) -> None:
            job.progress_pct = pct
            job.downloaded_bytes = downloaded
            job.total_bytes = total
            job.speed_bytes_sec = speed
            job.eta_seconds = eta
            await self.db.save_job(job)
            await self._notify(job, progress_handler)

        primary_name = (
            "audio_stream" if job.media_kind == "audio" else
            "source_asset" if job.media_kind == "asset" else "video_stream"
        )
        video_path = self.file_mgr.get_job_file_path(
            job.job_id, f"{primary_name}.{safe_extension(video.ext)}"
        )
        if not video_path.exists():
            status = JobStatus.DOWNLOADING_AUDIO if job.media_kind == "audio" else JobStatus.DOWNLOADING_VIDEO
            label = "Downloading original audio" if job.media_kind == "audio" else (
                "Downloading original source asset" if job.media_kind == "asset" else "Downloading video"
            )
            await self._set_status(job, status, label)
            video_path = await self.downloader.download_format(
                job.job_id,
                job.source_url or job.canonical_url or "",
                job.video_format_id,
                video_path.name,
                video.effective_size,
            video_progress,
            pid_update,
            "download-video",
            direct_source=job.extractor == "direct",
            raw_http=job.media_kind == "asset",
        )
        if not audio:
            return video_path
        audio_path = self.file_mgr.get_job_file_path(
            job.job_id, f"audio_stream.{safe_extension(audio.ext)}"
        )
        if not audio_path.exists():
            await self._set_status(job, JobStatus.DOWNLOADING_AUDIO, "Downloading audio")
            await self.downloader.download_format(
                job.job_id,
                job.source_url or job.canonical_url or "",
                job.audio_format_id or "",
                audio_path.name,
                audio.effective_size,
                video_progress,
                pid_update,
                "download-audio",
            )
        return video_path

    async def _execution_formats(
        self, job: DownloadJob
    ) -> tuple[MediaFormat, Optional[MediaFormat]]:
        if not job.video_format_snapshot:
            session = await self.db.get_media_session(job.media_session_id)
            if not session:
                raise ValueError("Job has no retained execution snapshot")
            video = session.get_format_by_id(job.video_format_id)
            audio = session.get_format_by_id(job.audio_format_id) if job.audio_format_id else None
            if not video or (job.audio_format_id and not audio):
                raise ExactFormatUnavailableError()
        else:
            video = MediaFormat.model_validate(job.video_format_snapshot)
            audio = (
                MediaFormat.model_validate(job.audio_format_snapshot)
                if job.audio_format_snapshot
                else None
            )
        factory = (
            self.extractor_registry.get_extractor_for_url
            if self.extractor_registry else self.extractor_factory
        )
        if factory and job.source_url and job.media_kind != "asset":
            lease, reason = await self.governor.acquire_stage("extraction")
            if lease is None:
                raise _ExtractionResourcesUnavailable(reason)
            async with lease:
                extractor = await factory(job.source_url)
                refreshed: MediaSession = await extractor.extract(
                    job.source_url, job.user_id, operation_id=job.job_id
                )
            current_video = refreshed.get_format_by_id(job.video_format_id)
            current_audio = refreshed.get_format_by_id(job.audio_format_id) if job.audio_format_id else None
            if not current_video or (job.audio_format_id and not current_audio):
                raise ExactFormatUnavailableError()
            self._assert_material_identity(video, current_video)
            if audio and current_audio:
                self._assert_material_identity(audio, current_audio)
            video, audio = current_video, current_audio
        return video, audio

    @staticmethod
    def _assert_material_identity(expected: MediaFormat, current: MediaFormat) -> None:
        keys = (
            "format_id",
            "vcodec_raw",
            "vcodec_normalized",
            "acodec_raw",
            "acodec_normalized",
            "width",
            "height",
            "fps",
            "ext",
            "is_video",
            "is_audio",
            "is_muxed",
        )
        if any(getattr(expected, key) != getattr(current, key) for key in keys):
            raise ExactFormatUnavailableError()

    def _projected_reservation(
        self, video: MediaFormat, audio: Optional[MediaFormat], job_id: str
    ) -> int:
        download_bytes, _ = calculate_total_download_size(video, audio)
        if download_bytes is not None:
            output_bytes = download_bytes if audio else 0
            return int((download_bytes + output_bytes) * 1.1)
        free = self.file_mgr.get_free_disk_space()
        safe = self.governor.disk_safety_bytes()
        usable = max(0, free - safe)
        share = 0.25 if self.settings.resource_mode == ResourceMode.AUTO_SHARED else 0.6
        budget = int(usable * share)
        if self.settings.max_job_size_bytes:
            budget = min(budget, self.settings.max_job_size_bytes)
        return budget
