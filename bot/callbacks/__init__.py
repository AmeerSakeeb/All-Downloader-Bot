"""Server-side callback workflow for pagination, selection, and cancellation."""

from __future__ import annotations

from typing import Any, cast

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.config import Settings
from core.models import JobStatus
from extractors.format_manager import select_default_audio
from jobqueue.manager import QueueManager
from jobqueue.scheduler import JobScheduler
from storage.database import Database, QueueLimitError
from services.telegram_api import TelegramService
from services.selection import submit_exact_selection
from ui.builders import build_audio_keyboard, build_format_page_keyboard, build_progress_keyboard

router = Router(name="phase1-callbacks")


async def _authorized(callback: CallbackQuery, db: Database) -> bool:
    user = callback.from_user
    record = await db.get_user(user.id)
    if not record or not record["is_allowed"]:
        await callback.answer("Access denied.", show_alert=True)
        return False
    return True


async def _owned_session(callback: CallbackQuery, db: Database, session_id: str):
    if not await _authorized(callback, db):
        return None
    session = await db.get_media_session(session_id)
    if not session or session.user_id != callback.from_user.id or session.is_expired():
        await callback.answer("This format menu is stale. Send the link again.", show_alert=True)
        return None
    return session


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery, db: Database) -> None:
    if await _authorized(callback, db):
        await callback.answer()


@router.callback_query(F.data.startswith("page:"))
async def callback_page(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Invalid menu action.", show_alert=True)
        return
    session = await _owned_session(callback, db, parts[1])
    if not session:
        return
    try:
        page = int(parts[2])
    except ValueError:
        await callback.answer("Invalid page.", show_alert=True)
        return
    if callback.message:
        message = cast(Any, callback.message)
        await message.edit_reply_markup(
            reply_markup=build_format_page_keyboard(session, page)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("fmt:"))
async def callback_format(
    callback: CallbackQuery,
    db: Database,
    scheduler: JobScheduler,
    settings: Settings,
    telegram_service: TelegramService,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Invalid format selection.", show_alert=True)
        return
    session = await _owned_session(callback, db, parts[1])
    if not session:
        return
    selected = session.get_format_by_key(parts[2])
    if not selected or not selected.is_video:
        await callback.answer("That exact format is no longer available.", show_alert=True)
        return
    preferences = await db.get_user_settings(callback.from_user.id)
    if selected.requires_separate_audio and not preferences.automatic_audio:
        if callback.message:
            await cast(Any, callback.message).edit_text(
                "🎵 <b>Select an exact audio stream</b>\n\nNo audio conversion will occur.",
                reply_markup=build_audio_keyboard(session, selected.internal_key),
            )
        await callback.answer()
        return
    audio = select_default_audio(session.formats, selected) if selected.requires_separate_audio else None
    if selected.requires_separate_audio and audio is None:
        await callback.answer("No companion audio stream is available.", show_alert=True)
        return
    allowed, reason = telegram_service.can_deliver_selection(selected, audio)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return
    if not callback.message:
        await callback.answer("The progress message is unavailable.", show_alert=True)
        return
    message = cast(Any, callback.message)
    try:
        result = await submit_exact_selection(
            db=db,
            scheduler=scheduler,
            telegram_service=telegram_service,
            app_settings=settings,
            session=session,
            primary=selected,
            audio=audio,
            user_id=callback.from_user.id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except QueueLimitError as error:
        await callback.answer(str(error), show_alert=True)
        return
    except ValueError:
        await callback.answer("This menu is stale. Please resend the link.", show_alert=True)
        return
    if result.disposition == "cache":
        await message.edit_text("✅ <b>Delivered from private cache</b>\n\nThe exact selected output was reused.")
        await callback.answer("Delivered")
    elif result.disposition == "coalesced":
        await message.edit_text(
            "🔗 <b>Joined an identical active download</b>\n\nYou will receive the exact output when ready.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="❌ Cancel my delivery", callback_data=f"cancel_sub:{result.subscriber_id}"
            )]]),
        )
        await callback.answer("Joined active download")
    else:
        assert result.job is not None
        await message.edit_text(
            "⏳ <b>Queued</b>\n\nWaiting for safe resource admission.",
            reply_markup=build_progress_keyboard(result.job.job_id),
        )
        await callback.answer("Download queued")


@router.callback_query(F.data.startswith("cancel_session:"))
async def callback_cancel_session(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer("Invalid action.", show_alert=True)
        return
    session = await _owned_session(callback, db, parts[1])
    if not session:
        return
    await db.delete_media_session(session.session_id, callback.from_user.id)
    if callback.message:
        await cast(Any, callback.message).edit_text("Selection cancelled.")
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_job:"))
async def callback_cancel_job(
    callback: CallbackQuery,
    db: Database,
    queue_mgr: QueueManager,
    scheduler: JobScheduler,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 2 or not await _authorized(callback, db):
        return
    job = await db.get_job(parts[1])
    if not job or job.user_id != callback.from_user.id:
        await callback.answer("This job is unavailable.", show_alert=True)
        return
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        await callback.answer("This job is already finished.", show_alert=True)
        return
    await scheduler.cancel_running_job(job.job_id, callback.from_user.id)
    if callback.message:
        await cast(Any, callback.message).edit_text("⏹️ <b>Cancelled</b>")
    await callback.answer("Cancelled")
