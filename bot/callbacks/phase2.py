"""Phase 2 settings, discovery, collections, auxiliary media and admin callbacks."""

from __future__ import annotations

import asyncio
import uuid
from html import escape
from typing import Any, cast

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.config import ResourceMode, Settings
from core.models import (
    AudioCodec, DetailStyle, FavoriteFormatRule, MatchingStrategy, MediaFormat,
    MediaSession, VideoCodec,
)
from extractors.format_manager import select_default_audio
from extractors.registry import ExtractorRegistry
from jobqueue.scheduler import JobScheduler
from resources.governor import ResourceGovernor
from security.ssrf import SSRFGuard
from services.favorites import FavoriteMatcher
from services.selection import SelectionResult, submit_exact_selection
from services.telegram_api import TelegramService
from storage.database import Database, QueueLimitError
from ui.builders import (
    build_admin_keyboard, build_admin_status_text, build_all_formats_keyboard,
    build_assets_keyboard, build_audio_keyboard, build_collection_keyboard,
    build_collection_text, build_favorites_keyboard, build_favorites_text,
    build_filter_keyboard, build_format_details_keyboard, build_format_details_text,
    build_item_selection_keyboard, build_media_info_details, build_preferred_keyboard,
    build_preferred_media_text, build_resource_keyboard, build_rule_choices,
    build_rule_editor, build_settings_keyboard, build_settings_text, build_users_keyboard,
    build_user_details, format_button_label,
)

router = Router(name="phase2-callbacks")


async def _authorized(callback: CallbackQuery, db: Database) -> bool:
    user = await db.get_user(callback.from_user.id)
    if not user or not user["is_allowed"]:
        await callback.answer("Access denied.", show_alert=True)
        return False
    return True


async def _admin(callback: CallbackQuery, db: Database) -> bool:
    user = await db.get_user(callback.from_user.id)
    if not user or not user["is_allowed"] or not user["is_admin"]:
        await callback.answer("Administrator access required.", show_alert=True)
        return False
    return True


async def _owned(callback: CallbackQuery, db: Database, session_id: str) -> MediaSession | None:
    if not await _authorized(callback, db):
        return None
    session = await db.get_media_session(session_id)
    if not session or session.user_id != callback.from_user.id or session.is_expired():
        await callback.answer("This media menu is stale. Send the link again.", show_alert=True)
        return None
    return session


async def _preferred(session: MediaSession, db: Database, page: int = 0):
    preferences = await db.get_user_settings(session.user_id)
    rules = await db.ensure_default_favorite_rules(session.user_id)
    result = FavoriteMatcher.match(session.formats, rules, preferences.matching_strategy)
    return (
        build_preferred_media_text(session, result, page),
        build_preferred_keyboard(session, result, page),
    )


async def _render_submission(callback: CallbackQuery, result: SelectionResult) -> None:
    if not callback.message:
        return
    message = cast(Any, callback.message)
    if result.disposition == "cache":
        await message.edit_text("✅ <b>Delivered from private cache</b>\n\nExact output reused.")
    elif result.disposition == "coalesced":
        await message.edit_text(
            "🔗 <b>Joined an identical active download</b>\n\nYou will receive the exact output when ready.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="❌ Cancel my delivery", callback_data=f"cancel_sub:{result.subscriber_id}"
            )]]),
        )
    else:
        assert result.job
        await message.edit_text(
            "⏳ <b>Queued</b>\n\nWaiting for safe resource admission.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="❌ Cancel my delivery", callback_data=f"cancel_sub:{result.subscriber_id}"
            )]]),
        )


async def _submit(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings, session: MediaSession,
    primary: MediaFormat, audio: MediaFormat | None = None, media_kind: str = "video",
) -> None:
    if not callback.message:
        await callback.answer("The status message is unavailable.", show_alert=True)
        return
    message = cast(Any, callback.message)
    try:
        result = await submit_exact_selection(
            db=db, scheduler=scheduler, telegram_service=telegram_service,
            app_settings=settings, session=session, primary=primary, audio=audio,
            user_id=callback.from_user.id, chat_id=message.chat.id,
            message_id=message.message_id, media_kind=media_kind,
        )
    except (QueueLimitError, ValueError) as error:
        await callback.answer(str(error), show_alert=True)
        return
    await _render_submission(callback, result)
    await callback.answer("Ready")


@router.callback_query(F.data.startswith("preferred:"))
async def preferred(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    session = await _owned(callback, db, parts[1]) if len(parts) in {2, 3} else None
    if session and callback.message:
        page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
        text, markup = await _preferred(session, db, page)
        await cast(Any, callback.message).edit_text(text, reply_markup=markup)
        await callback.answer()


@router.callback_query(F.data.startswith(("detail:", "adetail:")))
async def details(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    fmt = session.get_format_by_key(parts[2])
    if not fmt or not fmt.is_video:
        await callback.answer("That exact format is unavailable.", show_alert=True)
        return
    preferences = await db.get_user_settings(callback.from_user.id)
    manual_audio_required = fmt.requires_separate_audio and not preferences.automatic_audio
    audio = (
        select_default_audio(session.formats, fmt)
        if fmt.requires_separate_audio and preferences.automatic_audio else None
    )
    if callback.message:
        await cast(Any, callback.message).edit_text(
            build_format_details_text(
                fmt, audio, preferences.detail_style.value,
                manual_audio_required=manual_audio_required,
            ),
            reply_markup=build_format_details_keyboard(
                session, fmt, back="all" if parts[0] == "adetail" else "preferred",
                manual_audio_required=manual_audio_required,
            ),
        )
    await callback.answer()


async def _filters(db: Database, user_id: int, session_id: str) -> dict[str, Any]:
    draft_key = f"filters:{session_id}"
    draft = await db.get_ui_draft(user_id, draft_key)
    if not draft or draft.get("kind") != "filters" or draft.get("session_id") != session_id:
        draft = {"kind": "filters", "session_id": session_id, "filters": {}}
        await db.save_ui_draft(user_id, draft_key, draft)
    return draft["filters"]


@router.callback_query(F.data.startswith("all:"))
async def all_formats(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    filters = await _filters(db, callback.from_user.id, session.session_id)
    count = len([fmt for fmt in session.formats if fmt.is_video])
    if callback.message:
        await cast(Any, callback.message).edit_text(
            f"🎞 <b>Browse All Formats</b>\n\n{count} genuine video formats retained. Filters change presentation only.",
            reply_markup=build_all_formats_keyboard(session, filters, int(parts[2])),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def filter_menu(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[2] not in {"codec", "resolution", "fps", "container"}:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    filters = await _filters(db, callback.from_user.id, parts[1])
    selected = filters.get(parts[2]) or ["any"]
    await cast(Any, callback.message).edit_text(
        f"🔎 <b>{parts[2].title()} filter</b>\n\nSelect one or more values.",
        reply_markup=build_filter_keyboard(parts[1], parts[2], selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ftoggle:"))
async def filter_toggle(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    filters = await _filters(db, callback.from_user.id, parts[1])
    values = list(filters.get(parts[2]) or ["any"])
    value = parts[3]
    if value == "any":
        values = ["any"]
    else:
        values = [item for item in values if item != "any"]
        values = [item for item in values if item != value] if value in values else values + [value]
        values = values or ["any"]
    filters[parts[2]] = values
    await db.save_ui_draft(
        callback.from_user.id, f"filters:{parts[1]}",
        {"kind": "filters", "session_id": parts[1], "filters": filters},
    )
    await cast(Any, callback.message).edit_reply_markup(
        reply_markup=build_filter_keyboard(parts[1], parts[2], values)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("freset:"))
async def filter_reset(callback: CallbackQuery, db: Database) -> None:
    session_id = (callback.data or "").split(":", 1)[-1]
    session = await _owned(callback, db, session_id)
    if session:
        await db.save_ui_draft(
            callback.from_user.id, f"filters:{session_id}",
            {"kind": "filters", "session_id": session_id, "filters": {}},
        )
        await cast(Any, callback.message).edit_reply_markup(
            reply_markup=build_all_formats_keyboard(session, {}, 0)
        )
        await callback.answer("Filters reset")


@router.callback_query(F.data.startswith("search:"))
async def search_formats(callback: CallbackQuery, db: Database) -> None:
    session_id = (callback.data or "").split(":", 1)[-1]
    session = await _owned(callback, db, session_id)
    if not session:
        return
    filters = await _filters(db, callback.from_user.id, session_id)
    await db.save_ui_draft(callback.from_user.id, "format-search", {
        "kind": "format_search", "session_id": session_id,
    })
    await cast(Any, callback.message).edit_text(
        "🔎 <b>Search formats</b>\n\nSend a codec, resolution, FPS, container or exact format ID."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("audio:"))
async def audio_only(callback: CallbackQuery, db: Database) -> None:
    session = await _owned(callback, db, (callback.data or "").split(":", 1)[-1])
    if session and callback.message:
        await cast(Any, callback.message).edit_text(
            "🎵 <b>Audio Only</b>\n\nChoose an original source audio stream. No MP3 conversion occurs.",
            reply_markup=build_audio_keyboard(session),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("audopts:"))
async def audio_options(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) == 3:
        session = await _owned(callback, db, parts[1])
        if session and callback.message:
            await cast(Any, callback.message).edit_text(
                "🎵 <b>Audio Options</b>\n\nChoose an exact genuine stream for lossless merge.",
                reply_markup=build_audio_keyboard(session, parts[2]),
            )
            await callback.answer()


@router.callback_query(F.data.startswith("fmta:"))
async def exact_audio(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    video, audio = session.get_format_by_key(parts[2]), session.get_format_by_key(parts[3])
    if not video or not video.is_video or not audio or not audio.is_audio or audio.is_video:
        await callback.answer("The exact stream is unavailable.", show_alert=True)
        return
    await _submit(callback, db, scheduler, telegram_service, settings, session, video, audio)


@router.callback_query(F.data.startswith("aonly:"))
async def exact_audio_only(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    session = await _owned(callback, db, parts[1])
    audio = session.get_format_by_key(parts[2]) if session else None
    if not session or not audio or not audio.is_audio or audio.is_video:
        await callback.answer("That audio stream is unavailable.", show_alert=True)
        return
    await _submit(callback, db, scheduler, telegram_service, settings, session, audio, media_kind="audio")


@router.callback_query(F.data.startswith("assetfmt:"))
async def direct_asset_format(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    session = await _owned(callback, db, parts[1])
    fmt = session.get_format_by_key(parts[2]) if session else None
    if not session or not fmt:
        await callback.answer("That original asset is unavailable.", show_alert=True)
        return
    await _submit(callback, db, scheduler, telegram_service, settings, session, fmt, media_kind="asset")


@router.callback_query(F.data.startswith("info:"))
async def media_info(callback: CallbackQuery, db: Database) -> None:
    session = await _owned(callback, db, (callback.data or "").split(":", 1)[-1])
    if session and callback.message:
        await cast(Any, callback.message).edit_text(
            build_media_info_details(session),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔙 Back", callback_data=f"preferred:{session.session_id}"
            )]]),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("assets:"))
async def assets(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    session = await _owned(callback, db, parts[1])
    if not session:
        return
    values = session.thumbnails if parts[2] == "thumb" else session.subtitles
    if not values:
        await callback.answer("No source assets are available.", show_alert=True)
        return
    await cast(Any, callback.message).edit_text(
        "🖼 <b>Source Thumbnails</b>" if parts[2] == "thumb" else "💬 <b>Source Subtitles</b>",
        reply_markup=build_assets_keyboard(session, parts[2]),
    )
    await callback.answer()


def _asset_session(parent: MediaSession, asset, user_id: int) -> tuple[MediaSession, MediaFormat]:
    fmt = MediaFormat(
        format_id="direct", internal_key=asset.asset_id, is_video=False,
        is_audio=False, is_muxed=False, requires_separate_audio=False,
        vcodec_normalized=VideoCodec.NONE, acodec_normalized=AudioCodec.NONE,
        ext=asset.ext, filesize=asset.filesize, format_note=f"Original {asset.kind}",
    )
    session = MediaSession.with_ttl(
        ttl_seconds=max(60, int(parent.expires_at - parent.created_at)),
        user_id=user_id, url=asset.source_url, canonical_url=asset.source_url,
        extractor="direct", title=asset.label or asset.kind.title(), formats=[fmt],
    )
    return session, fmt


@router.callback_query(F.data.startswith("asset:"))
async def asset_download(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings,
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    parent = await _owned(callback, db, parts[1])
    if not parent:
        return
    asset = next((item for item in parent.thumbnails + parent.subtitles if item.asset_id == parts[2]), None)
    if not asset:
        await callback.answer("That source asset is unavailable.", show_alert=True)
        return
    await asyncio.to_thread(SSRFGuard.validate_url, asset.source_url)
    session, fmt = _asset_session(parent, asset, callback.from_user.id)
    await db.save_media_session(session)
    await _submit(callback, db, scheduler, telegram_service, settings, session, fmt, media_kind="asset")


@router.callback_query((F.data == "settings") | F.data.startswith("settings:"))
async def settings_panel(callback: CallbackQuery, db: Database) -> None:
    if not await _authorized(callback, db):
        return
    parts = (callback.data or "").split(":", 1)
    media_session_id = parts[1] if len(parts) == 2 else None
    if media_session_id and not await _owned(callback, db, media_session_id):
        return
    if media_session_id:
        await db.save_ui_draft(
            callback.from_user.id, "favorites-context",
            {"media_session_id": media_session_id},
        )
    else:
        await db.delete_ui_draft(callback.from_user.id, "favorites-context")
    settings = await db.get_user_settings(callback.from_user.id)
    await cast(Any, callback.message).edit_text(
        build_settings_text(settings),
        reply_markup=build_settings_keyboard(settings, media_session_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("set:"))
async def settings_update(callback: CallbackQuery, db: Database) -> None:
    if not await _authorized(callback, db):
        return
    parts = (callback.data or "").split(":")
    if len(parts) not in {3, 4}:
        return
    media_session_id = parts[3] if len(parts) == 4 else None
    if media_session_id and not await _owned(callback, db, media_session_id):
        return
    current = await db.get_user_settings(callback.from_user.id)
    update: dict[str, Any] = {}
    if parts[1] == "send" and parts[2] in {"video", "document"}:
        update["send_mode"] = parts[2]
    elif parts[1] == "strategy":
        update["matching_strategy"] = MatchingStrategy(parts[2])
    elif parts[1] == "detail":
        update["detail_style"] = DetailStyle(parts[2])
    elif parts[1] == "auto_audio":
        update["automatic_audio"] = not current.automatic_audio
    if update:
        current = current.model_copy(update=update)
        await db.save_user_settings(current)
    await cast(Any, callback.message).edit_text(
        build_settings_text(current),
        reply_markup=build_settings_keyboard(current, media_session_id),
    )
    await callback.answer("Saved")


@router.callback_query(F.data == "favorites")
async def favorites(callback: CallbackQuery, db: Database) -> None:
    if not await _authorized(callback, db):
        return
    rules = await db.ensure_default_favorite_rules(callback.from_user.id)
    context = await db.get_ui_draft(callback.from_user.id, "favorites-context")
    media_session_id = str(context.get("media_session_id")) if context and context.get("media_session_id") else None
    await cast(Any, callback.message).edit_text(
        build_favorites_text(rules),
        reply_markup=build_favorites_keyboard(rules, media_session_id),
    )
    await callback.answer()


@router.callback_query(F.data == "fadd")
async def favorite_add(callback: CallbackQuery, db: Database) -> None:
    if not await _authorized(callback, db):
        return
    rules = await db.list_favorite_rules(callback.from_user.id)
    rule = FavoriteFormatRule(user_id=callback.from_user.id, name=f"Rule {len(rules)+1}", priority=len(rules))
    await db.save_favorite_rule(rule)
    await cast(Any, callback.message).edit_text(f"✏️ <b>{escape(rule.name)}</b>", reply_markup=build_rule_editor(rule))
    await callback.answer("Rule added")


@router.callback_query(F.data.startswith("fedit:"))
async def favorite_edit(callback: CallbackQuery, db: Database) -> None:
    rule = await db.get_favorite_rule((callback.data or "").split(":", 1)[-1], callback.from_user.id)
    if not rule or not await _authorized(callback, db):
        await callback.answer("Rule unavailable.", show_alert=True)
        return
    await cast(Any, callback.message).edit_text(
        f"✏️ <b>{escape(rule.name)}</b>\n\nChoose a category or rule action.",
        reply_markup=build_rule_editor(rule),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("frcat:"))
async def favorite_category(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    rule = await db.get_favorite_rule(parts[1], callback.from_user.id) if len(parts) == 3 else None
    if not rule or parts[2] not in {"codec", "resolution", "fps", "container"}:
        return
    await cast(Any, callback.message).edit_text(
        f"✏️ <b>{parts[2].title()}</b>\n\nAny is mutually exclusive with specific values.",
        reply_markup=build_rule_choices(rule, parts[2]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("frtoggle:"))
async def favorite_toggle(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    rule = await db.get_favorite_rule(parts[1], callback.from_user.id)
    field = {"codec": "codecs", "resolution": "resolutions", "fps": "fps_values", "container": "containers"}.get(parts[2])
    if not rule or not field:
        return
    values = list(getattr(rule, field))
    value = parts[3]
    wildcard = value == "any" or (parts[2] == "fps" and value == "best")
    if wildcard:
        values = [value]
    else:
        values = [item for item in values if item not in {"any", "best"}]
        values = [item for item in values if item != value] if value in values else values + [value]
        values = values or ["any"]
    rule = rule.model_copy(update={field: values})
    await db.save_favorite_rule(rule)
    await cast(Any, callback.message).edit_reply_markup(reply_markup=build_rule_choices(rule, parts[2]))
    await callback.answer("Saved")


@router.callback_query(F.data.startswith("frop:"))
async def favorite_operation(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        return
    rule = await db.get_favorite_rule(parts[1], callback.from_user.id)
    if not rule:
        return
    action = parts[2]
    if action == "dup":
        await db.duplicate_favorite_rule(rule.rule_id, rule.user_id)
    elif action == "toggle":
        await db.save_favorite_rule(rule.model_copy(update={"enabled": not rule.enabled}))
    elif action in {"up", "down"}:
        await db.reorder_favorite_rule(rule.rule_id, rule.user_id, -1 if action == "up" else 1)
    elif action == "delete":
        await db.delete_favorite_rule(rule.rule_id, rule.user_id)
    rules = await db.ensure_default_favorite_rules(callback.from_user.id)
    context = await db.get_ui_draft(callback.from_user.id, "favorites-context")
    media_session_id = str(context.get("media_session_id")) if context and context.get("media_session_id") else None
    await cast(Any, callback.message).edit_text(
        build_favorites_text(rules),
        reply_markup=build_favorites_keyboard(rules, media_session_id),
    )
    await callback.answer("Updated")


@router.callback_query(F.data.startswith("cancel_sub:"))
async def cancel_subscription(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler
) -> None:
    subscriber_id = (callback.data or "").split(":", 1)[-1]
    job_id = await db.cancel_subscriber(subscriber_id, callback.from_user.id)
    if job_id:
        if await db.count_waiting_subscribers(job_id) == 0:
            await scheduler.cancel_running_job(job_id)
        await cast(Any, callback.message).edit_text("⏹ <b>Your delivery was cancelled</b>")
        await callback.answer("Cancelled")
    else:
        await callback.answer("This delivery is no longer waiting.", show_alert=True)


async def _child_session(
    parent: MediaSession, item, user_id: int, registry: ExtractorRegistry,
    governor: ResourceGovernor,
) -> MediaSession | None:
    if item.kind == "image":
        class Asset:
            asset_id = item.item_id
            kind = "image"
            source_url = item.source_url
            ext = item.source_url.split("?", 1)[0].rsplit(".", 1)[-1] or "jpg"
            label = item.title
            filesize = None
        return _asset_session(parent, Asset(), user_id)[0]
    if item.formats:
        uses_parent = not item.webpage_url and bool(item.parent_collection_url)
        return MediaSession.with_ttl(
            ttl_seconds=max(60, int(parent.expires_at - parent.created_at)),
            user_id=user_id, url=item.source_url, canonical_url=item.source_url,
            extractor=parent.extractor, title=item.title, media_id=item.extractor_id,
            thumbnail_url=item.thumbnail_url, formats=item.formats,
            parent_collection_url=item.parent_collection_url if uses_parent else None,
            collection_entry_index=item.collection_entry_index if uses_parent else None,
            collection_entry_id=item.collection_entry_id if uses_parent else None,
            ytdlp_impersonated=parent.ytdlp_impersonated,
        )
    lease, _ = await governor.acquire_stage("extraction")
    if not lease:
        return None
    async with lease:
        await asyncio.to_thread(SSRFGuard.validate_url, item.source_url)
        extractor = await registry.get_extractor_for_url(item.source_url)
        return await extractor.extract(
            item.source_url, user_id, operation_id=f"collection-{uuid.uuid4().hex}",
            **(
                {"prefer_impersonation": True}
                if parent.ytdlp_impersonated else {}
            ),
        )


@router.callback_query(F.data.startswith("collection:"))
async def collection(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    session = await _owned(callback, db, parts[1]) if len(parts) in {2, 3} else None
    if session:
        await cast(Any, callback.message).edit_text(
            build_collection_text(session),
            reply_markup=build_collection_keyboard(session, int(parts[2]) if len(parts) == 3 else 0),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("allmedia:"))
async def prepare_download_all(
    callback: CallbackQuery, db: Database, extractor_registry: ExtractorRegistry,
    governor: ResourceGovernor,
) -> None:
    session = await _owned(callback, db, (callback.data or "").split(":", 1)[-1])
    if not session or not callback.message:
        return
    preferences = await db.get_user_settings(callback.from_user.id)
    rules = await db.ensure_default_favorite_rules(callback.from_user.id)
    choices: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    lines = [
        "📦 <b>Download All Media</b>", "",
        "Configured Favorite rules determine each exact video stream.", "",
    ]
    for index, item in enumerate(session.items, 1):
        child = await _child_session(
            session, item, callback.from_user.id, extractor_registry, governor
        )
        if not child:
            lines.append(f"{index}. ⏳ Temporarily unavailable")
            continue
        await db.save_media_session(child)
        if item.kind == "image" and child.formats:
            fmt = child.formats[0]
            choices.append({
                "session_id": child.session_id, "primary_key": fmt.internal_key,
                "audio_key": None, "media_kind": "asset", "title": item.title,
            })
            lines.append(f"{index}. 🖼 Original image")
            continue
        if child.items:
            missing.append({
                "session_id": child.session_id, "title": item.title,
                "reason": "collection",
            })
            lines.append(f"{index}. 📚 Item selection required")
            continue
        matches = FavoriteMatcher.match(
            child.formats, rules, preferences.matching_strategy
        )
        primary = matches.formats[0] if matches.formats else None
        if not primary:
            missing.append({
                "session_id": child.session_id, "title": item.title,
                "reason": "format",
            })
            lines.append(f"{index}. 🎞 Format selection required")
            continue
        audio = None
        if primary.requires_separate_audio:
            if preferences.automatic_audio:
                audio = select_default_audio(child.formats, primary)
            if audio is None:
                missing.append({
                    "session_id": child.session_id, "title": item.title,
                    "reason": "audio", "primary_key": primary.internal_key,
                })
                lines.append(f"{index}. 🎞 Audio selection required")
                continue
        choices.append({
            "session_id": child.session_id, "primary_key": primary.internal_key,
            "audio_key": audio.internal_key if audio else None,
            "media_kind": "video", "title": item.title,
        })
        lines.append(f"{index}. 🎞 {escape(format_button_label(primary))}")
    lines.extend(("", f"Ready to queue: {len(choices)}", f"Needs selection: {len(missing)}"))
    await db.save_ui_draft(callback.from_user.id, f"download-all:{session.session_id}", {
        "kind": "download_all", "session_id": session.session_id,
        "choices": choices, "missing": missing,
    })
    buttons: list[list[InlineKeyboardButton]] = []
    if choices:
        buttons.append([InlineKeyboardButton(
            text="▶️ Queue available items", callback_data=f"allqueue:{session.session_id}"
        )])
    if missing:
        buttons.append([InlineKeyboardButton(
            text="🎞 Choose missing formats", callback_data=f"allmissing:{session.session_id}"
        )])
    buttons.append([InlineKeyboardButton(
        text="❌ Cancel", callback_data=f"allcancel:{session.session_id}"
    )])
    await cast(Any, callback.message).edit_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer("Exact selections prepared")


@router.callback_query(F.data.startswith("allmissing:"))
async def choose_missing_formats(callback: CallbackQuery, db: Database) -> None:
    session_id = (callback.data or "").split(":", 1)[-1]
    if not await _owned(callback, db, session_id) or not callback.message:
        return
    draft = await db.get_ui_draft(callback.from_user.id, f"download-all:{session_id}")
    if not draft or draft.get("kind") != "download_all" or draft.get("session_id") != session_id:
        await callback.answer("This confirmation has expired.", show_alert=True)
        return
    for missing in draft.get("missing") or []:
        child = await db.get_media_session(str(missing.get("session_id") or ""))
        if not child or child.user_id != callback.from_user.id or child.is_expired():
            continue
        if child.items:
            await cast(Any, callback.message).answer(
                build_collection_text(child), reply_markup=build_collection_keyboard(child)
            )
        elif missing.get("reason") == "audio" and missing.get("primary_key"):
            await cast(Any, callback.message).answer(
                f"🎵 <b>{escape(str(missing.get('title') or child.title))}</b>\n\nChoose an exact audio stream.",
                reply_markup=build_audio_keyboard(child, str(missing["primary_key"])),
            )
        else:
            text, markup = await _preferred(child, db)
            await cast(Any, callback.message).answer(text, reply_markup=markup)
    await callback.answer("Selection panels opened")


@router.callback_query(F.data.startswith("allqueue:"))
async def queue_download_all(
    callback: CallbackQuery, db: Database, scheduler: JobScheduler,
    telegram_service: TelegramService, settings: Settings,
) -> None:
    session_id = (callback.data or "").split(":", 1)[-1]
    if not await _owned(callback, db, session_id) or not callback.message:
        return
    draft = await db.get_ui_draft(callback.from_user.id, f"download-all:{session_id}")
    if not draft or draft.get("kind") != "download_all" or draft.get("session_id") != session_id:
        await callback.answer("This confirmation has expired.", show_alert=True)
        return
    queued = 0
    failed = 0
    message = cast(Any, callback.message)
    for choice in draft.get("choices") or []:
        child = await db.get_media_session(str(choice.get("session_id") or ""))
        primary = child.get_format_by_key(str(choice.get("primary_key") or "")) if child else None
        audio = child.get_format_by_key(str(choice["audio_key"])) if child and choice.get("audio_key") else None
        if not child or child.user_id != callback.from_user.id or child.is_expired() or not primary:
            failed += 1
            continue
        status_message = await message.answer(
            f"⏳ <b>{escape(str(choice.get('title') or child.title))}</b>\n\nPreparing exact selection."
        )
        try:
            result = await submit_exact_selection(
                db=db, scheduler=scheduler, telegram_service=telegram_service,
                app_settings=settings, session=child, primary=primary, audio=audio,
                user_id=callback.from_user.id, chat_id=status_message.chat.id,
                message_id=status_message.message_id,
                media_kind=str(choice.get("media_kind") or "video"),
            )
            if result.disposition == "cache":
                await status_message.edit_text("✅ <b>Delivered from private cache</b>")
            else:
                label = "Joined identical active download" if result.disposition == "coalesced" else "Queued"
                await status_message.edit_text(
                    f"⏳ <b>{label}</b>",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                        text="❌ Cancel my delivery", callback_data=f"cancel_sub:{result.subscriber_id}"
                    )]]),
                )
            queued += 1
        except (QueueLimitError, ValueError):
            failed += 1
            await status_message.edit_text("❌ This exact item could not be queued.")
    await db.delete_ui_draft(callback.from_user.id, f"download-all:{session_id}")
    await message.edit_text(
        f"📦 <b>Download All Media</b>\n\nQueued/delivered: {queued}\nNot queued: {failed}"
    )
    await callback.answer("Available exact selections processed")


@router.callback_query(F.data.startswith("allcancel:"))
async def cancel_download_all(callback: CallbackQuery, db: Database) -> None:
    session_id = (callback.data or "").split(":", 1)[-1]
    await db.delete_ui_draft(callback.from_user.id, f"download-all:{session_id}")
    if callback.message:
        await cast(Any, callback.message).edit_text("Download All Media cancelled.")
    await callback.answer("Cancelled")


@router.callback_query(F.data.startswith("item:"))
async def collection_item(
    callback: CallbackQuery, db: Database, extractor_registry: ExtractorRegistry,
    governor: ResourceGovernor,
) -> None:
    parts = (callback.data or "").split(":")
    parent = await _owned(callback, db, parts[1]) if len(parts) == 3 else None
    item = next((value for value in parent.items if value.item_id == parts[2]), None) if parent else None
    if not parent or not item:
        return
    child = await _child_session(parent, item, callback.from_user.id, extractor_registry, governor)
    if not child:
        await callback.answer("The server is busy. Try this item again shortly.", show_alert=True)
        return
    await db.save_media_session(child)
    if item.kind == "image":
        fmt = child.formats[0]
        # Reuse the asset queue route without exposing the URL in callback data.
        await cast(Any, callback.message).edit_text(
            f"🖼 <b>{escape(item.title)}</b>\n\nOriginal image source; no re-encoding.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="⬇️ Download original", callback_data=f"assetfmt:{child.session_id}:{fmt.internal_key}"
            )], [InlineKeyboardButton(text="🔙 Back", callback_data=f"collection:{parent.session_id}")]]),
        )
    elif child.items:
        await cast(Any, callback.message).edit_text(
            build_collection_text(child), reply_markup=build_collection_keyboard(child)
        )
    else:
        text, markup = await _preferred(child, db)
        await cast(Any, callback.message).edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("iselect:"))
async def select_items(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    session = await _owned(callback, db, parts[1]) if len(parts) in {2, 3} else None
    if session:
        page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
        draft_key = f"items:{session.session_id}"
        draft = await db.get_ui_draft(callback.from_user.id, draft_key)
        if not draft or draft.get("kind") != "items" or draft.get("session_id") != session.session_id:
            draft = {"kind": "items", "session_id": session.session_id, "selected": []}
            await db.save_ui_draft(callback.from_user.id, draft_key, draft)
        await cast(Any, callback.message).edit_text(
            "☑ <b>Select media items</b>",
            reply_markup=build_item_selection_keyboard(session, list(draft.get("selected") or []), page),
        )
        await callback.answer()


@router.callback_query(F.data.startswith("items:"))
async def select_first(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    session = await _owned(callback, db, parts[1]) if len(parts) == 3 else None
    if not session:
        return
    count = len(session.items) if parts[2] == "all" else min(int(parts[2]), len(session.items))
    selected = [item.item_id for item in session.items[:count]]
    await db.save_ui_draft(
        callback.from_user.id, f"items:{session.session_id}",
        {"kind": "items", "session_id": session.session_id, "selected": selected},
    )
    await cast(Any, callback.message).edit_text(
        f"☑ <b>{count} items selected</b>", reply_markup=build_item_selection_keyboard(session, selected)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("itoggle:"))
async def item_toggle(callback: CallbackQuery, db: Database) -> None:
    parts = (callback.data or "").split(":")
    session = await _owned(callback, db, parts[1]) if len(parts) == 4 else None
    draft_key = f"items:{parts[1]}"
    draft = await db.get_ui_draft(callback.from_user.id, draft_key)
    if not session or not draft or draft.get("kind") != "items" or draft.get("session_id") != parts[1]:
        return
    selected = list(draft.get("selected") or [])
    selected = [item for item in selected if item != parts[2]] if parts[2] in selected else selected + [parts[2]]
    draft["selected"] = selected
    await db.save_ui_draft(callback.from_user.id, draft_key, draft)
    page = int(parts[3]) if parts[3].isdigit() else 0
    await cast(Any, callback.message).edit_reply_markup(
        reply_markup=build_item_selection_keyboard(session, selected, page)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("iprocess:"))
async def process_selected(
    callback: CallbackQuery, db: Database, extractor_registry: ExtractorRegistry,
    governor: ResourceGovernor,
) -> None:
    session = await _owned(callback, db, (callback.data or "").split(":", 1)[-1])
    draft_key = f"items:{session.session_id}" if session else "items:missing"
    draft = await db.get_ui_draft(callback.from_user.id, draft_key)
    selected = set(draft.get("selected") or []) if draft and draft.get("kind") == "items" else set()
    if not session or not selected:
        await callback.answer("Select at least one item.", show_alert=True)
        return
    await cast(Any, callback.message).edit_text(
        "📚 <b>Preparing selected items</b>\n\nEach video keeps its own exact-format workflow."
    )
    for item in (item for item in session.items if item.item_id in selected):
        child = await _child_session(session, item, callback.from_user.id, extractor_registry, governor)
        if not child:
            continue
        await db.save_media_session(child)
        if item.kind == "image":
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="⬇️ Download original", callback_data=f"assetfmt:{child.session_id}:{child.formats[0].internal_key}"
            )]])
            await cast(Any, callback.message).answer(f"🖼 <b>{escape(item.title)}</b>", reply_markup=markup)
        elif child.items:
            await cast(Any, callback.message).answer(
                build_collection_text(child), reply_markup=build_collection_keyboard(child)
            )
        else:
            text, markup = await _preferred(child, db)
            await cast(Any, callback.message).answer(text, reply_markup=markup)
    await db.delete_ui_draft(callback.from_user.id, draft_key)
    await callback.answer("Prepared")


async def _show_admin(
    callback: CallbackQuery, db: Database, governor: ResourceGovernor,
    scheduler: JobScheduler,
) -> None:
    sample = await governor.detector.sample()
    limits = await governor.adaptive_limits()
    stats = await db.phase2_stats()
    text = build_admin_status_text(
        sample, stats, limits, governor.settings.resource_mode.value,
        scheduler.paused, governor.file_mgr.get_free_disk_space(),
    )
    await cast(Any, callback.message).edit_text(text, reply_markup=build_admin_keyboard(scheduler.paused))


@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(
    callback: CallbackQuery, db: Database, governor: ResourceGovernor,
    scheduler: JobScheduler,
) -> None:
    if not await _admin(callback, db):
        return
    action = (callback.data or "").split(":", 1)[-1]
    if action == "pause":
        scheduler.pause_new_jobs()
    elif action == "resume":
        scheduler.resume_new_jobs()
    elif action == "cleanup":
        await db.delete_expired_sessions()
    elif action == "users":
        users = await db.list_users()
        await cast(Any, callback.message).edit_text(
            "👥 <b>Authorized Users</b>\n\nTap a non-owner user to allow/suspend.",
            reply_markup=build_users_keyboard(users),
        )
        await callback.answer()
        return
    elif action == "resources":
        await cast(Any, callback.message).edit_text(
            "⚙️ <b>Resource Mode</b>\n\nAuto Shared protects other services and remains the default.",
            reply_markup=build_resource_keyboard(governor.settings.resource_mode.value),
        )
        await callback.answer()
        return
    await _show_admin(callback, db, governor, scheduler)
    await callback.answer("Updated")


@router.callback_query(F.data.startswith("resource:"))
async def resource_mode(
    callback: CallbackQuery, db: Database, governor: ResourceGovernor,
) -> None:
    if not await _admin(callback, db):
        return
    mode = ResourceMode((callback.data or "").split(":", 1)[-1])
    governor.settings.resource_mode = mode
    await db.set_system_setting("resource_mode", mode.value)
    await cast(Any, callback.message).edit_reply_markup(reply_markup=build_resource_keyboard(mode.value))
    await callback.answer("Resource mode saved")


@router.callback_query(F.data.startswith("user:"))
async def user_toggle(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not await _admin(callback, db):
        return
    parts = (callback.data or "").split(":")
    target = int(parts[1]) if len(parts) == 3 else 0
    user = await db.get_user(target)
    if user and parts[2] == "view":
        text, markup = build_user_details(user, await db.user_job_stats(target))
        await cast(Any, callback.message).edit_text(text, reply_markup=markup)
        await callback.answer()
        return
    if not user or target in settings.admin_user_ids or user["is_admin"]:
        await callback.answer("Configured administrators cannot be changed here.", show_alert=True)
        return
    await db.add_or_update_user(target, is_admin=False, is_allowed=not user["is_allowed"])
    await cast(Any, callback.message).edit_reply_markup(reply_markup=build_users_keyboard(await db.list_users()))
    await callback.answer("User updated")
