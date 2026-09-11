"""Secure exact-selection admission, cache reuse and active-job coalescing."""

from dataclasses import dataclass
from typing import Optional

import aiosqlite

from core.config import Settings
from core.models import DownloadJob, MediaFormat, MediaSession
from extractors.format_manager import select_default_audio
from jobqueue.scheduler import JobScheduler
from services.output_identity import build_output_identity
from services.telegram_api import TelegramService
from storage.database import Database, QueueLimitError


@dataclass(frozen=True)
class SelectionResult:
    disposition: str
    job: Optional[DownloadJob] = None
    subscriber_id: Optional[str] = None


async def submit_exact_selection(
    *,
    db: Database,
    scheduler: JobScheduler,
    telegram_service: TelegramService,
    app_settings: Settings,
    session: MediaSession,
    primary: MediaFormat,
    user_id: int,
    chat_id: int,
    message_id: Optional[int],
    audio: Optional[MediaFormat] = None,
    media_kind: str = "video",
) -> SelectionResult:
    if session.user_id != user_id or session.is_expired():
        raise ValueError("Media session is stale or not owned by this user")
    preferences = await db.get_user_settings(user_id)
    send_mode = preferences.send_mode if media_kind == "video" else "document"
    if media_kind == "video" and primary.requires_separate_audio and audio is None:
        if not preferences.automatic_audio:
            raise ValueError("Manual audio selection is required for this video format")
        audio = select_default_audio(session.formats, primary)
    if media_kind == "video" and primary.requires_separate_audio and audio is None:
        raise ValueError("No companion audio stream is available")
    allowed, reason = telegram_service.can_deliver_selection(primary, audio)
    if not allowed:
        raise QueueLimitError(reason)
    identity = build_output_identity(
        session, primary, audio, send_mode, media_kind=media_kind
    )
    async def deliver_cached() -> bool:
        cached = await db.get_cached_file(identity)
        if not cached:
            return False
        try:
            await telegram_service.send_cached(
                chat_id, cached["telegram_file_id"], send_mode,
                "Served from the bot's private exact-format cache.",
            )
            return True
        except Exception:
            await db.invalidate_cached_file(identity)
            return False

    # The partial unique job index, transactional subscriber admission and
    # atomic upload finalizer may race in either safe direction. Retry only a
    # small fixed number of times so a terminal job can become cache/new work.
    for _ in range(3):
        if await deliver_cached():
            return SelectionResult("cache")
        active = await db.find_active_job_by_identity(identity)
        if active:
            subscriber_id = await db.try_add_job_subscriber(
                active.job_id, identity, user_id, chat_id, message_id, send_mode,
                app_settings.max_queued_jobs_per_user,
            )
            if subscriber_id:
                return SelectionResult(
                    "coalesced", job=active, subscriber_id=subscriber_id
                )
            continue
        try:
            job = await db.create_download_job(
                session=session,
                video_format=primary,
                audio_format=audio,
                chat_id=chat_id,
                message_id=message_id,
                send_mode=send_mode,
                media_kind=media_kind,
                output_identity=identity,
                max_queued_jobs_per_user=app_settings.max_queued_jobs_per_user,
                max_total_queued_jobs=app_settings.max_total_queued_jobs,
            )
        except aiosqlite.IntegrityError:
            continue
        scheduler.wake()
        recipient = await db.get_waiting_subscriber(
            job.job_id, user_id, chat_id, message_id
        )
        return SelectionResult(
            "queued", job=job,
            subscriber_id=recipient["subscriber_id"] if recipient else None,
        )
    if await deliver_cached():
        return SelectionResult("cache")
    raise QueueLimitError("The active download changed. Please try this selection again.")
