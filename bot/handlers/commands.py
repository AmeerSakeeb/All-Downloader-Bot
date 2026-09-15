"""Basic commands and admin allowlist management handlers."""

import logging
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from storage.database import Database
from core.config import Settings
from jobqueue.scheduler import JobScheduler
from resources.governor import ResourceGovernor
from ui.builders import (
    build_admin_keyboard, build_admin_status_text, build_settings_keyboard,
    build_settings_text, build_home_keyboard, build_home_text,
    build_queue_keyboard, build_queue_text, build_help_keyboard, build_help_text,
)

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("start"))
async def cmd_start(
    message: Message, db: Database, user_db: dict, governor: ResourceGovernor,
):
    """Compact operational home screen."""
    if not message.from_user:
        return
    jobs = await db.get_active_jobs_for_user(message.from_user.id)
    active_statuses = {
        "claimed", "downloading_video", "downloading_audio", "merging", "uploading",
    }
    active = sum(job.status.value in active_statuses for job in jobs)
    await message.reply(
        build_home_text(
            active=active, waiting=len(jobs) - active,
            download_target=governor.settings.max_concurrent_downloads,
            mode=governor.settings.resource_mode.value,
            max_links=governor.settings.max_batch_urls,
        ),
        reply_markup=build_home_keyboard(
            is_admin=bool(user_db.get("is_admin")),
            max_links=governor.settings.max_batch_urls,
        ),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, user_db: dict):
    """Open the same Help Center used by inline navigation."""
    await message.reply(
        build_help_text("main"),
        reply_markup=build_help_keyboard("main", is_admin=bool(user_db.get("is_admin"))),
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, db: Database):
    if not message.from_user:
        return
    preferences = await db.get_user_settings(message.from_user.id)
    await db.delete_ui_draft(message.from_user.id, "favorites-context")
    await message.reply(
        build_settings_text(preferences), reply_markup=build_settings_keyboard(preferences)
    )


@router.message(Command("queue"))
async def cmd_queue(message: Message, db: Database, user_db: dict):
    if not message.from_user:
        return
    jobs = await db.get_active_jobs_for_user(message.from_user.id)
    positions = {}
    for job in jobs:
        position = await db.queue_position(job.job_id)
        if position is not None:
            positions[job.job_id] = position
    await message.reply(
        build_queue_text(jobs, positions),
        reply_markup=build_queue_keyboard(jobs, admin=bool(user_db.get("is_admin"))),
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, db: Database, user_db: dict, governor: ResourceGovernor,
    scheduler: JobScheduler,
):
    if not user_db.get("is_admin"):
        return
    sample = await governor.detector.sample()
    limits = await governor.adaptive_limits()
    stats = await db.phase2_stats()
    await message.reply(
        build_admin_status_text(
            sample, stats, limits, governor.settings.resource_mode.value,
            scheduler.paused, governor.file_mgr.get_free_disk_space(),
            getattr(getattr(scheduler, "telegram_service", None), "capabilities", None),
            (
                getattr(
                    getattr(getattr(scheduler, "extractor_registry", None), "compatibility_overrides", None),
                    "ytdlp_version", "unavailable",
                ),
                getattr(
                    getattr(getattr(scheduler, "extractor_registry", None), "compatibility_overrides", None),
                    "enabled_count", 0,
                ),
            ),
        ),
        reply_markup=build_admin_keyboard(scheduler.paused),
    )


# =========================================================================
# Admin Command Handlers
# =========================================================================

@router.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject, db: Database, user_db: dict):
    """Admin command: Allow a user_id access."""
    if not user_db.get("is_admin"):
        return

    if not command.args:
        await message.reply("⚠️ Usage: <code>/allow &lt;user_id&gt;</code>")
        return

    try:
        target_id = int(command.args.strip())
        await db.add_or_update_user(target_id, is_admin=False, is_allowed=True)
        await message.reply(f"✅ User <code>{target_id}</code> has been authorized.")
        actor_id = message.from_user.id if message.from_user else 0
        logger.info("Admin %s authorized user %s", actor_id, target_id)
    except ValueError:
        await message.reply("⚠️ Invalid User ID format. Please specify an integer ID.")
    except Exception:
        logger.exception("Unable to authorize user")
        await message.reply("❌ The user could not be authorized.")


@router.message(Command("disallow"))
async def cmd_disallow(
    message: Message, command: CommandObject, db: Database, user_db: dict, settings: Settings
):
    """Admin command: Revoke a user_id's access."""
    if not user_db.get("is_admin"):
        return

    if not command.args:
        await message.reply("⚠️ Usage: <code>/disallow &lt;user_id&gt;</code>")
        return

    try:
        target_id = int(command.args.strip())
        actor_id = message.from_user.id if message.from_user else 0
        if target_id == actor_id or target_id in settings.admin_user_ids:
            await message.reply("⚠️ A configured administrative owner cannot be disallowed.")
            return

        await db.add_or_update_user(target_id, is_admin=False, is_allowed=False)
        await message.reply(f"🛑 Access revoked for user <code>{target_id}</code>.")
        logger.info("Admin %s revoked access for user %s", actor_id, target_id)
    except ValueError:
        await message.reply("⚠️ Invalid User ID format. Please specify an integer ID.")
    except Exception:
        logger.exception("Unable to revoke user")
        await message.reply("❌ Access could not be revoked.")


@router.message(Command("listusers"))
async def cmd_list_users(message: Message, db: Database, user_db: dict):
    """Admin command: List all registered users and status."""
    if not user_db.get("is_admin"):
        return

    try:
        users = await db.list_users()
        if not users:
            await message.reply("No users registered in database.")
            return

        lines = ["👥 <b>Registered users</b>"]
        for u in users:
            admin_tag = "👑 Admin" if u["is_admin"] else "User"
            allowed_tag = "✅ Allowed" if u["is_allowed"] else "🛑 Suspended"
            lines.append(f"• <code>{u['user_id']}</code>: {allowed_tag} ({admin_tag})")

        await message.reply("\n".join(lines))
    except Exception:
        logger.exception("Unable to list users")
        await message.reply("❌ Users could not be listed.")
