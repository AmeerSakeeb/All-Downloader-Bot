"""Handlers for media URL recognition, validation, extraction, and format list display."""

import asyncio
import logging
import uuid
from html import escape
from urllib.parse import urlsplit
from aiogram import Router, F
from aiogram.types import Message

from bot.batch_manager import BatchAnalysisManager, global_batch_manager
from core.exceptions import BotError, ExtractionError, SecurityError
from core.models import MediaItem, MediaSession
from extractors.registry import ExtractorRegistry
from jobqueue.scheduler import JobScheduler
from security.ssrf import SSRFGuard
from security.url_logging import sanitize_url_for_log
from resources.governor import ResourceGovernor
from storage.database import Database
from services.favorites import FavoriteMatcher
from ui.builders import (
    build_all_formats_keyboard, build_collection_keyboard, build_collection_text,
    build_all_formats_text, build_analysis_error_keyboard,
    build_error_text, build_new_download_keyboard,
    build_preferred_keyboard, build_preferred_media_text,
)

logger = logging.getLogger(__name__)
router = Router()


def extract_url_from_text(text: str) -> str:
    """Extract first http or https URL found in the message text."""
    if not text:
        return ""
    # Look for http:// or https://
    for word in text.split():
        if word.startswith(("http://", "https://")):
            # Basic sanity clean
            return word.strip()
    return ""


def extract_urls_from_text(text: str) -> list[str]:
    values: list[str] = []
    for word in (text or "").split():
        candidate = word.strip("<>[](){}.,;\"'")
        if candidate.startswith(("http://", "https://")) and candidate not in values:
            values.append(candidate)
    return values


async def _wait_for_extraction_lease(
    governor: ResourceGovernor, db: Database, user_id: int
):
    """Wait fairly for adaptive capacity without asking the user to resend."""
    while True:
        lease, reason = await governor.acquire_stage("extraction")
        if lease is not None:
            return lease
        user = await db.get_user(user_id)
        if not user or not user["is_allowed"]:
            return None
        await asyncio.sleep(max(1.0, min(10.0, governor.settings.scheduler_retry_seconds)))


def _platform_label(url: str) -> str:
    host = (urlsplit(url).hostname or "Media").lower()
    host = host.removeprefix("www.")
    return host.split(".")[0].replace("-", " ").title() or "Media"


async def _analyze_batch(
    *, session: MediaSession, message: Message, status_msg, db: Database,
    governor: ResourceGovernor, extractor_registry: ExtractorRegistry,
    batch_manager: BatchAnalysisManager,
) -> None:
    update_lock = asyncio.Lock()

    async def publish() -> None:
        async with update_lock:
            if batch_manager.is_cancelled(session.session_id):
                return
            await db.save_media_session(session)
            try:
                view_draft = await db.get_ui_draft(
                    session.user_id, f"batch-view:{session.session_id}"
                )
                current_view = (view_draft or {}).get("view", "status")
                if current_view != "status":
                    return
                await status_msg.edit_text(
                    build_collection_text(session),
                    reply_markup=build_collection_keyboard(session),
                )
            except Exception:
                logger.debug("Batch status edit was rejected or unchanged", exc_info=True)

    async def analyze(item: MediaItem) -> None:
        item.analysis_status = "waiting"
        await publish()
        try:
            lease = await _wait_for_extraction_lease(
                governor, db, message.from_user.id
            )
            if lease is None:
                item.analysis_status = "failed"
                item.analysis_error_category = "access_revoked"
                await publish()
                return
            item.analysis_status = "analyzing"
            await publish()
            async with lease:
                if batch_manager.is_cancelled(session.session_id):
                    return
                extractor = await extractor_registry.get_extractor_for_url(item.source_url)
                child = await extractor.extract(
                    item.source_url,
                    message.from_user.id,
                    operation_id=f"batch-{session.session_id}-{item.item_id}",
                )
            item.title = child.title or item.title
            item.formats = child.formats
            item.thumbnail_url = child.thumbnail_url
            item.duration = child.duration
            item.extractor_id = child.media_id
            item.source_extractor = child.extractor
            item.analysis_status = "ready"
            await publish()
        except BotError as error:
            item.analysis_status = "failed"
            item.analysis_error_category = getattr(error, "error_category", "media_unavailable")
            await publish()
        except Exception:
            logger.exception("Batch child analysis failed")
            item.analysis_status = "failed"
            item.analysis_error_category = "unexpected_error"
            await publish()
        finally:
            pass

    await asyncio.gather(*(analyze(item) for item in session.items))


@router.message(F.text)
async def handle_potential_url(
    message: Message, db: Database, governor: ResourceGovernor,
    extractor_registry: ExtractorRegistry, scheduler: JobScheduler,
):
    """Processes incoming messages to see if they contain valid, authorized media URLs."""
    if message.from_user is None:
        return
    text = message.text or ""
    urls = extract_urls_from_text(text)
    if not urls:
        pending_search = await db.get_latest_ui_draft(
            message.from_user.id, "format_search:"
        )
        search_key, draft = pending_search if pending_search else ("", None)
        if draft and draft.get("kind") == "format_search":
            session = await db.get_media_session(str(draft.get("session_id") or ""))
            if session and session.user_id == message.from_user.id and not session.is_expired():
                draft_key = f"format_filters:{session.session_id}"
                filter_draft = await db.get_ui_draft(message.from_user.id, draft_key)
                filters = dict((filter_draft or {}).get("filters") or {})
                filters["query"] = text.strip()[:80]
                await db.save_ui_draft(message.from_user.id, draft_key, {
                    "kind": "filters", "session_id": session.session_id, "filters": filters,
                })
                await db.delete_ui_draft(message.from_user.id, search_key)
                await message.reply(
                    build_all_formats_text(session, filters, 0),
                    reply_markup=build_all_formats_keyboard(session, filters, 0),
                )
            else:
                await db.delete_ui_draft(message.from_user.id, search_key)
                await message.reply(
                    "⌛ <b>This format search expired</b>\n\nSend the media link again to start a new search.",
                    reply_markup=build_new_download_keyboard(),
                )
        return

    pending_search = await db.get_latest_ui_draft(
        message.from_user.id, "format_search:"
    )
    if pending_search:
        await db.delete_ui_draft(message.from_user.id, pending_search[0])

    if scheduler.paused:
        await message.reply(
            "⏸ <b>New downloads are paused</b>\n\n"
            "The administrator has temporarily paused admission."
        )
        return
    user_active = await db.count_active_jobs_for_user(message.from_user.id)
    total_active = await db.count_total_active_jobs()
    if user_active >= governor.settings.max_queued_jobs_per_user:
        await message.reply(
            "📋 <b>Queue full</b>\n\nYour link was not accepted.\n\n"
            f"Current: {user_active} / {governor.settings.max_queued_jobs_per_user} "
            "active or queued for your account."
        )
        return
    if total_active >= governor.settings.max_total_queued_jobs:
        await message.reply(
            "📋 <b>Queue full</b>\n\nYour link was not accepted because the global queue is full."
        )
        return
    if governor.file_mgr.get_free_disk_space() <= governor.disk_safety_bytes():
        await message.reply(
            "💾 <b>Storage safety pause</b>\n\n"
            "Your link was not accepted while protected disk headroom is unavailable."
        )
        return

    # 1. SSRF Validation Gate 1
    try:
        if len(urls) > governor.settings.max_batch_urls:
            await message.reply(
                f"⚠️ <b>Too many links</b>\n\n"
                f"{governor.settings.max_batch_urls} links can be submitted at once. "
                f"You sent {len(urls)}.\n\nNo links were discarded or submitted."
            )
            return
        for candidate in urls:
            await asyncio.to_thread(SSRFGuard.validate_url, candidate)
    except SecurityError as e:
        logger.warning("Blocked unsafe URL batch for user %s", message.from_user.id)
        await message.reply(f"⚠️ <b>Unsafe URL Blocked</b>\n\n{e.user_message}")
        return
    except BotError as e:
        await message.reply(f"⚠️ <b>Invalid URL</b>\n\n{e.user_message}")
        return

    if len(urls) > 1:
        session = MediaSession.with_ttl(
            ttl_seconds=governor.settings.media_session_ttl,
            user_id=message.from_user.id,
            url=urls[0],
            extractor="batch",
            title=f"{len(urls)} URLs found",
            session_kind="batch",
            items=[MediaItem(
                kind="video", source_url=value, title=_platform_label(value),
                analysis_status="waiting",
            ) for value in urls],
        )
        await db.save_media_session(session)
        status_msg = await message.reply(
            build_collection_text(session), reply_markup=build_collection_keyboard(session)
        )
        batch_manager = global_batch_manager
        batch_manager.start(session.session_id, _analyze_batch(
            session=session, message=message, status_msg=status_msg, db=db,
            governor=governor, extractor_registry=extractor_registry,
            batch_manager=batch_manager,
        ))
        return

    url = urls[0]

    # Persist admission before waiting so temporary pressure never loses the
    # accepted request or requires a resend.
    pending = MediaSession.with_ttl(
        ttl_seconds=governor.settings.media_session_ttl,
        user_id=message.from_user.id, url=url, canonical_url=url,
        extractor="pending", title=_platform_label(url), formats=[],
    )
    await db.save_media_session(pending)

    # Notify user extraction has begun
    status_msg = await message.reply(
        "🔎 <b>Analyzing link</b>\n\nChecking the original source qualities."
    )

    try:
        lease, reason = await governor.acquire_stage("extraction")
        if not lease:
            await status_msg.edit_text(
                "⏳ <b>Waiting for a safe analysis slot</b>\n\n"
                "Your link is saved and analysis will start automatically.\n\n"
                "You do not need to resend it."
            )
            lease = await _wait_for_extraction_lease(
                governor, db, message.from_user.id
            )
        if lease is None:
            await status_msg.edit_text("Access denied.")
            return
        async with lease:
            # Access may have been revoked during the bounded resource wait.
            user = await db.get_user(message.from_user.id)
            if not user or not user["is_allowed"]:
                await status_msg.edit_text("Access denied.")
                return
            extractor = await extractor_registry.get_extractor_for_url(url)
            session: MediaSession = await extractor.extract(
                url,
                message.from_user.id,
                operation_id=f"analysis-{uuid.uuid4().hex}",
            )
            session.session_id = pending.session_id
        
        # 4. Save session to SQLite
        await db.save_media_session(session)

        if session.items:
            response_text = build_collection_text(session)
            reply_markup = build_collection_keyboard(session)
        else:
            preferences = await db.get_user_settings(message.from_user.id)
            rules = await db.ensure_default_favorite_rules(message.from_user.id)
            matches = FavoriteMatcher.match(session.formats, rules, preferences.matching_strategy)
            response_text = build_preferred_media_text(
                session, matches, automatic_audio=preferences.automatic_audio,
            )
            reply_markup = build_preferred_keyboard(
                session, matches, automatic_audio=preferences.automatic_audio,
            )

        await status_msg.edit_text(
            text=response_text,
            reply_markup=reply_markup,
        )
        logger.info(f"Successful metadata extraction for session {session.session_id} (user {message.from_user.id})")

    except ExtractionError as e:
        logger.error("Extraction failed for %s", sanitize_url_for_log(url), exc_info=True)
        await status_msg.edit_text(
            build_error_text(getattr(e, "error_category", None)),
            reply_markup=build_analysis_error_keyboard(),
        )
    except SecurityError as e:
        logger.warning("Security error during extraction for %s", sanitize_url_for_log(url))
        await status_msg.edit_text(
            "🔗 <b>This link cannot be used</b>\n\nThe address did not pass the bot's link safety checks.",
            reply_markup=build_analysis_error_keyboard(),
        )
    except Exception as e:
        logger.error(f"Unexpected error during URL analysis: {e}", exc_info=True)
        await status_msg.edit_text(
            build_error_text(None), reply_markup=build_analysis_error_keyboard()
        )
