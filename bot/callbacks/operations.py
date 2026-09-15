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
from services.compatibility import CompatibilityOverrideRegistry
from services.telegram_capabilities import TelegramCapabilities

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
    back = [[("↩️ Back to admin", "admin:status")]]
    try:
        if action in {"cleanup", "backup", "cancel", "cancelwaiting", "toggle"}:
            token = uuid.uuid4().hex
            await db.save_ui_draft(callback.from_user.id, "admin-confirm:" + token, {
                "action": action, "arg": arg, "expires": time.time() + 120,
            })
            labels = {"cleanup": "🧹 Clean expired metadata and safe orphan files?",
                      "backup": "💾 Create a private database backup and rotate old backups?",
                      "cancel": "⏹ Cancel this shared download for every remaining recipient?",
                      "cancelwaiting": "⏹ Cancel every waiting download? Running downloads will continue.",
                      "toggle": "🔐 Change this site login session's enabled status?"}
            await callback.message.edit_text(labels[action], reply_markup=keyboard([
                [("✅ Confirm action", "ops:confirm:" + token), ("↩️ Keep current state", "ops:dismiss:" + token)],
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
            elif operation == "cancelwaiting":
                changed = await db.cancel_all_waiting_jobs()
                application.scheduler.wake()
                text = f"⏹ Cancelled {changed} waiting jobs. Running healthy jobs were left alone."
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
            governor = application.governor
            limits = await governor.adaptive_limits()
            targets = {
                "download": governor.settings.max_concurrent_downloads,
                "merge": governor.settings.max_concurrent_merges,
                "upload": governor.settings.max_concurrent_uploads,
            }
            page = max(0, int(arg)) if arg.isdigit() else 0
            page = min(page, max(0, (len(jobs)-1)//8))
            visible = jobs[page*8:page*8+8]
            rows = [[(
                f"⏹ Cancel download #{page * 8 + index} · user {j.user_id}",
                "ops:cancel:" + j.job_id,
            )] for index, j in enumerate(visible, 1)]
            if len(jobs) > 8:
                nav = []
                if page > 0:
                    nav.append(("◀ Previous", f"ops:queue:{page-1}"))
                if (page + 1) * 8 < len(jobs):
                    nav.append(("Next ▶", f"ops:queue:{page+1}"))
                if nav:
                    rows.append(nav)
            rows.append([(
                "▶️ Accept new downloads" if application.scheduler.paused else "⏸ Pause new downloads",
                "admin:resume" if application.scheduler.paused else "admin:pause",
            )])
            if any(job.status.value in {"queued", "waiting_resources"} for job in jobs):
                rows.append([("⏹ Cancel all waiting", "ops:cancelwaiting:all")])
            waiting = sum(job.status.value in {"queued", "waiting_resources"} for job in jobs)
            active = len(jobs) - waiting
            lines = [
                "📋 <b>All downloads</b>", "",
                f"Active: {active} · Waiting: {waiting} · Total: {len(jobs)}",
                f"Downloads: {limits['download']} / {targets['download']} effective / target",
                f"Merges: {limits['merge']} / {targets['merge']} effective / target",
                f"Uploads: {limits['upload']} / {targets['upload']} effective / target",
                f"Pressure: {escape(governor.current_pressure_reason(limits))}", "",
            ]
            for index, job in enumerate(visible, page * 8 + 1):
                lines.append(
                    f"#{index} · user <code>{job.user_id}</code> · "
                    f"{escape(job.current_stage or job.status.value)}"
                )
            lines.extend(("", "Select a job to confirm cancellation."))
            await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(rows+back))
        elif action in {"sessions", "refresh"}:
            if action == "refresh":
                await application.cookie_profiles.refresh()
            profiles = application.cookie_profiles
            lines = ["🔐 <b>Site login sessions</b>", "", "Operator-configured sessions for websites that require authorized access."]
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
                nav = []
                if page > 0:
                    nav.append(("◀ Previous", f"ops:sessions:{page-1}"))
                if (page + 1) * 4 < len(profiles.profiles):
                    nav.append(("Next ▶", f"ops:sessions:{page+1}"))
                if nav:
                    rows.append(nav)
            await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(rows+back))
        else:
            governor = application.governor
            capabilities = getattr(application, "telegram_capabilities", None)
            if capabilities is None:
                capabilities = TelegramCapabilities.from_settings(governor.settings)
            compatibility = getattr(application, "compatibility_overrides", None)
            if compatibility is None:
                compatibility = CompatibilityOverrideRegistry()
            sample = await governor.detector.sample()
            limits = await governor.adaptive_limits()
            stats = await db.phase2_stats()
            errors = await db.recent_error_counts()
            free = governor.file_mgr.get_free_disk_space()
            targets = {
                "extraction": governor.settings.max_concurrent_extractions,
                "download": governor.settings.max_concurrent_downloads,
                "merge": governor.settings.max_concurrent_merges,
                "upload": governor.settings.max_concurrent_uploads,
            }
            lines = ["📊 <b>System health</b>", f"Version: {__version__}",
                f"Uptime: {int(time.monotonic()-application.started_at)} seconds",
                f"Bot: {'✅ Healthy' if application.scheduler.is_alive and await db.ping() else '⚠️ Attention required'}",
                f"Admissions: {'Paused' if application.scheduler.paused else 'Running'}",
                f"Resource mode: {governor.settings.resource_mode.value}",
                "", "<b>Concurrency · effective / target</b>",
                f"Extraction: {limits['extraction']} / {targets['extraction']}",
                f"Downloads: {limits['download']} / {targets['download']}",
                f"Merges: {limits['merge']} / {targets['merge']}",
                f"Uploads: {limits['upload']} / {targets['upload']}",
                f"Reason: {escape(governor.current_pressure_reason(limits))}", "",
                f"Disk free: {free//1024**2} MiB · reserved: {(await db.get_total_reserved_bytes())//1024**2} MiB",
                f"Disk pressure: {free <= governor.disk_safety_bytes()}",
                f"Memory available: {sample['memory']['available']//1024**2} MiB",
                f"Active jobs: {stats['active']} · waiting: {len(await db.get_jobs_by_status('waiting_resources'))}",
                f"Cache hits: {stats['cache_hits']}", "",
                "<b>Telegram delivery</b>",
                *capabilities.status_lines(), "",
                "<b>Extractor engine</b>",
                f"yt-dlp {escape(compatibility.ytdlp_version)}",
                f"Compatibility overrides: {compatibility.enabled_count} enabled",
                "Generic extractor: available", "", "Recent errors (24h):"]
            lines.extend(f"{escape(k)}: {v}" for k,v in errors.items())
            await callback.message.edit_text("\n".join(lines), reply_markup=keyboard(back))
        await callback.answer()
    except Exception:
        logger.exception("Admin operation failed action=%s", action)
        await callback.message.edit_text("❌ Operation could not be completed. Check the sanitized operator logs.", reply_markup=keyboard(back))
        await callback.answer("Operation failed", show_alert=True)
