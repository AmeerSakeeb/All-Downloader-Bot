"""Admin-only diagnostics and confirmed runtime operations."""

import asyncio
import logging
import time
import uuid
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.version import __version__
from services.cookie_profiles import CookieProfileError

router = Router()
logger = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=data) for text, data in row
    ] for row in rows])


@router.callback_query(F.data.startswith("ops:"))
async def operations(callback: CallbackQuery, db, application) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or not user["is_admin"] or not user["is_allowed"]:
        await callback.answer("Administrator access required.", show_alert=True)
        return
    if not callback.message:
        await callback.answer("This menu is unavailable.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else ""
    back = [[("🔙 Admin", "admin:status")]]
    try:
        if action in {"cleanup", "backup", "cancel", "toggle"}:
            token = uuid.uuid4().hex
            await db.save_ui_draft(callback.from_user.id, "admin-confirm:" + token, {
                "action": action, "arg": arg, "expires": time.time() + 120,
            })
            labels = {"cleanup": "Clean expired metadata and safe orphan files?",
                      "backup": "Create a private database backup and rotate old backups?",
                      "cancel": "Cancel this shared job for every remaining recipient?",
                      "toggle": "Change this authorized session's enabled status?"}
            await callback.message.edit_text(labels[action], reply_markup=keyboard([
                [("Confirm", "ops:confirm:" + token), ("Cancel", "ops:dismiss:" + token)],
            ]))
        elif action == "dismiss":
            await db.delete_ui_draft(callback.from_user.id, "admin-confirm:" + arg)
            await callback.message.edit_text("Operation cancelled.", reply_markup=keyboard(back))
        elif action == "confirm":
            draft = await db.consume_ui_draft(callback.from_user.id, "admin-confirm:" + arg)
            if not draft or draft.get("expires", 0) < time.time():
                await callback.answer("Confirmation expired. Open the operation again.", show_alert=True)
                return
            await callback.answer("Working…")
            operation, target = draft["action"], draft["arg"]
            if operation == "cleanup":
                result = await application.maintenance.run()
                text = "🧹 Maintenance complete\n" + "\n".join(f"{escape(k)}: {v}" for k,v in result.items())
            elif operation == "backup":
                name = await application.maintenance.backup()
                text = "✅ Private backup created: <code>" + escape(name) + "</code>\nStored in the runtime backups directory."
            elif operation == "cancel":
                changed = await application.scheduler.cancel_running_job(target)
                text = "⏹ Job cancelled." if changed else "This job is already terminal or unavailable."
            elif operation == "toggle":
                profiles = application.cookie_profiles
                if target not in profiles.profiles:
                    raise CookieProfileError("Session profile no longer exists")
                await profiles.set_enabled(target, not profiles.enabled(target))
                text = "✅ Session override saved. In-flight requests finish with their current policy."
            else:
                text = "Operation unavailable."
            await callback.message.edit_text(text, reply_markup=keyboard(back))
            return
        elif action == "queue":
            jobs = await db.get_active_jobs()
            page = max(0, int(arg)) if arg.isdigit() else 0
            page = min(page, max(0, (len(jobs)-1)//8))
            rows = [[(f"⏹ {j.job_id[:8]} · {j.status.value}", "ops:cancel:" + j.job_id)] for j in jobs[page*8:page*8+8]]
            rows.append([("←", f"ops:queue:{max(0,page-1)}"), ("→", f"ops:queue:{page+1}")])
            await callback.message.edit_text(f"📋 Active queue: {len(jobs)}\nSelect a job to confirm cancellation.", reply_markup=keyboard(rows+back))
        elif action in {"sessions", "refresh"}:
            if action == "refresh":
                await application.cookie_profiles.refresh()
            profiles = application.cookie_profiles
            lines = ["🔐 <b>Authorized Sessions</b>"]
            rows = []
            page = max(0, int(arg)) if arg.isdigit() else 0
            page = min(page, max(0, (len(profiles.profiles)-1)//4))
            for profile in list(profiles.profiles.values())[page*4:page*4+4]:
                try:
                    await asyncio.to_thread(profile.validate_file)
                    status = "configured ✓" if profiles.enabled(profile.name) else "disabled"
                except CookieProfileError:
                    status = "file unavailable/invalid"
                lines.append(
                    f"\nProfile: <b>{escape(profile.name)}</b>\n"
                    f"Source domains: {escape(', '.join(profile.source_domains))}\n"
                    f"Cookie-domain scope: {escape(', '.join(profile.cookie_domains))}\n"
                    f"Status: {status}"
                )
                rows.append([(f"Enable/disable {profile.name}", "ops:toggle:" + profile.name)])
            if not profiles.profiles:
                lines.append("No operator session profiles configured.")
            rows.append([("Refresh configuration", "ops:refresh")])
            if len(profiles.profiles) > 4:
                rows.append([("←", f"ops:sessions:{max(0,page-1)}"), ("→", f"ops:sessions:{page+1}")])
            await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(rows+back))
        else:
            governor = application.governor
            sample = await governor.detector.sample()
            stats = await db.phase2_stats()
            errors = await db.recent_error_counts()
            free = governor.file_mgr.get_free_disk_space()
            lines = ["🩺 <b>Diagnostics</b>", f"Version: {__version__}",
                f"Uptime: {int(time.monotonic()-application.started_at)} seconds",
                f"Scheduler: {'paused' if application.scheduler.paused else 'running'} / alive={application.scheduler.is_alive}",
                f"Proxy: {application.proxy.is_alive} · DB: {await db.ping()}",
                f"Maintenance: {bool(application._maintenance_task and not application._maintenance_task.done())}",
                f"Resource mode: {governor.settings.resource_mode.value}",
                f"Disk free: {free//1024**2} MiB · reserved: {(await db.get_total_reserved_bytes())//1024**2} MiB",
                f"Disk pressure: {free <= governor.disk_safety_bytes()}",
                f"Memory available: {sample['memory']['available']//1024**2} MiB",
                f"Active jobs: {stats['active']} · waiting: {len(await db.get_jobs_by_status('waiting_resources'))}",
                f"Cache hits: {stats['cache_hits']}", "Recent errors (24h):"]
            lines.extend(f"{escape(k)}: {v}" for k,v in errors.items())
            await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(back))
        await callback.answer()
    except Exception:
        logger.exception("Admin operation failed action=%s", action)
        await callback.message.edit_text("❌ Operation could not be completed. Check the sanitized operator logs.", reply_markup=keyboard(back))
