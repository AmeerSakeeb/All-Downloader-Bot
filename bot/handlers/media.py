"""Handlers for media URL recognition, validation, extraction, and format list display."""

import asyncio
import logging
import uuid
from html import escape
from aiogram import Router, F
from aiogram.types import Message

from core.exceptions import BotError, ExtractionError, SecurityError
from core.models import MediaItem, MediaSession
from extractors.registry import ExtractorRegistry
from security.ssrf import SSRFGuard
from security.url_logging import sanitize_url_for_log
from resources.governor import ResourceGovernor
from storage.database import Database
from services.favorites import FavoriteMatcher
from ui.builders import (
    build_all_formats_keyboard, build_collection_keyboard, build_collection_text,
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


@router.message(F.text)
async def handle_potential_url(
    message: Message, db: Database, governor: ResourceGovernor,
    extractor_registry: ExtractorRegistry,
):
    """Processes incoming messages to see if they contain valid, authorized media URLs."""
    if message.from_user is None:
        return
    text = message.text or ""
    urls = extract_urls_from_text(text)
    if not urls:
        draft = await db.get_ui_draft(message.from_user.id)
        if draft and draft.get("kind") == "format_search":
            session = await db.get_media_session(str(draft.get("session_id") or ""))
            if session and session.user_id == message.from_user.id and not session.is_expired():
                filters = dict(draft.get("filters") or {})
                filters["query"] = text.strip()[:80]
                await db.save_ui_draft(message.from_user.id, {
                    "kind": "filters", "session_id": session.session_id, "filters": filters,
                })
                await message.reply(
                    f"🔎 <b>Format search:</b> {escape(filters['query'])}",
                    reply_markup=build_all_formats_keyboard(session, filters, 0),
                )
        return

    # 1. SSRF Validation Gate 1
    try:
        if len(urls) > governor.settings.max_batch_urls:
            await message.reply(
                f"⚠️ This message contains too many URLs. The safe batch limit is {governor.settings.max_batch_urls}."
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
            items=[MediaItem(kind="url", source_url=value, title=f"URL {index}") for index, value in enumerate(urls, 1)],
        )
        await db.save_media_session(session)
        await message.reply(
            build_collection_text(session), reply_markup=build_collection_keyboard(session)
        )
        return

    url = urls[0]

    # Notify user extraction has begun
    status_msg = await message.reply("🔍 <b>Analyzing link…</b>\n\nExtracting available formats.")

    try:
        lease, reason = await governor.acquire_stage("extraction")
        if not lease:
            await status_msg.edit_text(
                "⏳ Waiting briefly for extraction resources. Analysis will resume automatically."
            )
            deadline = asyncio.get_running_loop().time() + 30.0
            while lease is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(min(2.0, max(0, deadline - asyncio.get_running_loop().time())))
                lease, reason = await governor.acquire_stage("extraction")
        if lease is None:
            await status_msg.edit_text(
                "The server is busy. Please resend the link in a little while."
            )
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
        
        # 4. Save session to SQLite
        await db.save_media_session(session)

        if session.items:
            response_text = build_collection_text(session)
            reply_markup = build_collection_keyboard(session)
        else:
            preferences = await db.get_user_settings(message.from_user.id)
            rules = await db.ensure_default_favorite_rules(message.from_user.id)
            matches = FavoriteMatcher.match(session.formats, rules, preferences.matching_strategy)
            response_text = build_preferred_media_text(session, matches)
            reply_markup = build_preferred_keyboard(session, matches)

        await status_msg.edit_text(
            text=response_text,
            reply_markup=reply_markup,
        )
        logger.info(f"Successful metadata extraction for session {session.session_id} (user {message.from_user.id})")

    except ExtractionError as e:
        logger.error("Extraction failed for %s", sanitize_url_for_log(url), exc_info=True)
        await status_msg.edit_text(
            f"❌ <b>Extraction failed</b>\n\nUnable to retrieve format information for this link.\n"
            f"Reason: {escape(e.user_message)}"
        )
    except SecurityError as e:
        logger.warning("Security error during extraction for %s", sanitize_url_for_log(url))
        await status_msg.edit_text(f"⚠️ <b>Security error</b>\n\n{e.user_message}")
    except Exception as e:
        logger.error(f"Unexpected error during URL analysis: {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Unexpected error</b>\n\nCould not analyze this media link.")
