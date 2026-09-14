"""Fully asynchronous aiogram 3 application lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks import router as callbacks_router
from bot.callbacks.phase2 import router as phase2_callbacks_router
from bot.callbacks.operations import router as operations_router
from bot.handlers.commands import router as commands_router
from bot.handlers.media import router as media_router
from bot.middleware.access import AccessControlMiddleware
from bot.middleware.logging import LoggingMiddleware
from core.config import Settings, get_settings
from core.logging import setup_logging
from core.models import DownloadJob, JobStatus
from downloads.downloader import Downloader
from downloads.ffmpeg_manager import FFmpegManager
from downloads.process_supervisor import ProcessSupervisor
from extractors.registry import ExtractorRegistry
from jobqueue.manager import QueueManager
from jobqueue.scheduler import JobScheduler
from resources.governor import ResourceGovernor
from security.proxy import ControlledOutboundProxy
from services.telegram_api import TelegramService
from services.cookie_profiles import CookieProfiles
from services.maintenance import MaintenanceService
from services.output_identity import build_completed_output_identity
from services.runtime_settings import RuntimeSettingsService
from storage.database import Database
from storage.file_manager import FileManager
from ui.builders import build_error_keyboard, build_progress_text

logger = logging.getLogger(__name__)


class BotApplication:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        bot_factory: Callable[..., Bot] = Bot,
    ):
        self.settings = settings or get_settings()
        self.bot_factory = bot_factory
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.db: Optional[Database] = None
        self.file_mgr: Optional[FileManager] = None
        self.governor: Optional[ResourceGovernor] = None
        self.queue_mgr: Optional[QueueManager] = None
        self.scheduler: Optional[JobScheduler] = None
        self.telegram_service: Optional[TelegramService] = None
        self.supervisor: Optional[ProcessSupervisor] = None
        self.proxy: Optional[ControlledOutboundProxy] = None
        self.extractor_registry: Optional[ExtractorRegistry] = None
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._maintenance_task: Optional[asyncio.Task[None]] = None
        self.cookie_profiles: Optional[CookieProfiles] = None
        self.maintenance: Optional[MaintenanceService] = None
        self.runtime_settings: Optional[RuntimeSettingsService] = None
        self.started_at = time.monotonic()
        self._handler_tasks: set[asyncio.Task] = set()
        self._progress_last: dict[str, float] = {}
        self._progress_signature: dict[str, tuple[str, str, int]] = {}
        self._shutdown = False
        self.health_file = self.settings.data_dir / "health.json"

    async def initialize(self) -> None:
        self.settings.ensure_directories()
        setup_logging(
            self.settings.log_level,
            self.settings.log_dir,
            self.settings.log_max_bytes,
            self.settings.log_backup_count,
        )
        self.db = Database(self.settings.database_path, self.settings.default_send_mode.value)
        await self.db.connect()
        self.db.ui_draft_ttl_seconds = self.settings.ui_draft_ttl_hours * 3600
        await self.db.bootstrap_admins(self.settings.admin_user_ids)
        self.runtime_settings = RuntimeSettingsService(self.db, self.settings)
        await self.runtime_settings.initialize()
        # Import the pre-v10 resource-mode toggle once. New writes use the
        # typed runtime-settings table exclusively.
        saved_resource_mode = await self.db.get_system_setting("resource_mode")
        if saved_resource_mode and not self.runtime_settings.is_overridden("resource_mode"):
            await self.runtime_settings.set("resource_mode", saved_resource_mode, updated_by=0)
        self.file_mgr = FileManager(self.settings.jobs_dir)
        self.cookie_profiles = CookieProfiles(self.settings, self.db)
        await self.cookie_profiles.refresh()
        self.maintenance = MaintenanceService(self.db, self.file_mgr, self.settings)
        self.supervisor = ProcessSupervisor(
            self.settings.process_terminate_grace_seconds
        )
        self.proxy = ControlledOutboundProxy()
        proxy_url = await self.proxy.start()
        self.extractor_registry = ExtractorRegistry(
            settings=self.settings, supervisor=self.supervisor, proxy=self.proxy,
            profiles=self.cookie_profiles,
        )
        self.governor = ResourceGovernor(self.db, self.file_mgr, self.settings)
        downloader = Downloader(
            self.file_mgr,
            self.settings,
            supervisor=self.supervisor,
            proxy_url=proxy_url,
            growth_check=self.governor.growth_is_safe,
            profiles=self.cookie_profiles,
        )
        ffmpeg = FFmpegManager(self.supervisor)
        self.queue_mgr = QueueManager(self.db, self.file_mgr, self.supervisor)

        bot_kwargs: dict[str, Any] = {
            "default": DefaultBotProperties(parse_mode=ParseMode.HTML)
        }
        if self.settings.use_local_api:
            bot_kwargs["session"] = AiohttpSession(
                api=TelegramAPIServer.from_base(
                    self.settings.local_api_base_url, is_local=True
                )
            )
        self.bot = self.bot_factory(token=self.settings.bot_token, **bot_kwargs)
        self.telegram_service = TelegramService(self.bot, self.settings)

        async def upload(path: Path, job: DownloadJob) -> bool:
            assert self.telegram_service is not None
            telegram_file_id: Optional[str] = job.telegram_file_id
            output_container = path.suffix.lower().lstrip(".") or "bin"
            completed_identity = (
                build_completed_output_identity(job.output_identity, output_container)
                if job.output_identity else None
            )
            delivered_any = False
            for subscriber in await self.db.list_job_subscribers(job.job_id):
                if not await self.db.subscriber_is_waiting(subscriber["subscriber_id"]):
                    continue
                try:
                    if telegram_file_id:
                        try:
                            await self.telegram_service.send_cached(
                                subscriber["chat_id"], telegram_file_id,
                                subscriber["send_mode"],
                                "Exact output delivered from a shared private download.",
                            )
                        except Exception:
                            telegram_file_id = job.telegram_file_id = None
                            if completed_identity:
                                await self.db.invalidate_cached_file(completed_identity)
                    if not telegram_file_id:
                        delivery_job = job.model_copy(update={
                            "chat_id": subscriber["chat_id"],
                            "message_id": subscriber["message_id"],
                            "send_mode": subscriber["send_mode"],
                            "telegram_file_id": None,
                        })
                        await self.telegram_service.send_media(
                            delivery_job, path,
                            "Downloaded without re-encoding where streams required combining.",
                        )
                        if not delivery_job.telegram_file_id:
                            raise RuntimeError("Telegram did not return a reusable file identifier")
                        telegram_file_id = delivery_job.telegram_file_id
                        job.telegram_file_id = telegram_file_id
                        if completed_identity and job.output_identity:
                            await self.db.save_cached_file(
                                completed_identity, telegram_file_id, subscriber["send_mode"],
                                output_container, execution_identity=job.output_identity,
                            )
                except Exception:
                    logger.warning("Independent recipient delivery failed", exc_info=True)
                    await self.db.mark_subscriber(subscriber["subscriber_id"], "failed")
                    if subscriber["message_id"]:
                        try:
                            await self.bot.edit_message_text(
                                chat_id=subscriber["chat_id"], message_id=subscriber["message_id"],
                                text=("❌ <b>Telegram delivery failed</b>\n\n"
                                      "Telegram could not deliver the completed media. Please try again."),
                            )
                        except Exception:
                            logger.debug("Recipient delivery-failure edit was rejected", exc_info=True)
                    continue
                delivered_any = True
                await self.db.mark_subscriber(subscriber["subscriber_id"], "delivered")
                if subscriber["message_id"]:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=subscriber["chat_id"], message_id=subscriber["message_id"],
                            text="✅ <b>Download Complete</b>\n\nOriginal output delivered without re-encoding.",
                        )
                    except Exception:
                        logger.debug("Recipient completion edit was rejected", exc_info=True)
            return delivered_any

        self.scheduler = JobScheduler(
            self.db,
            self.file_mgr,
            self.governor,
            self.queue_mgr,
            downloader,
            ffmpeg,
            self.settings,
            upload_handler=upload,
            progress_handler=self._update_progress,
            extractor_registry=self.extractor_registry,
            telegram_service=self.telegram_service,
        )
        self.governor.set_capacity_listener(self.scheduler.wake)
        self.runtime_settings.add_listener(lambda _key, _value: self.scheduler.wake())
        await self.queue_mgr.reconcile_on_startup()
        if await self.db.get_system_setting("scheduler_paused") == "true":
            self.scheduler.pause_new_jobs()

        self.dp = Dispatcher()
        self.dp["db"] = self.db
        self.dp["application"] = self
        self.dp["settings"] = self.settings
        self.dp["resource_governor"] = self.governor
        self.dp["governor"] = self.governor
        self.dp["queue_mgr"] = self.queue_mgr
        self.dp["scheduler"] = self.scheduler
        self.dp["telegram_service"] = self.telegram_service
        self.dp["extractor_registry"] = self.extractor_registry
        self.dp["runtime_settings"] = self.runtime_settings
        access = AccessControlMiddleware(self.db, self.settings)
        logging_middleware = LoggingMiddleware(self._handler_tasks)
        self.dp.message.outer_middleware(access)
        self.dp.callback_query.outer_middleware(access)
        self.dp.message.middleware(logging_middleware)
        self.dp.callback_query.middleware(logging_middleware)
        self.dp.include_router(commands_router)
        self.dp.include_router(media_router)
        self.dp.include_router(phase2_callbacks_router)
        self.dp.include_router(operations_router)
        self.dp.include_router(callbacks_router)

        await self.maintenance.run()
        await self.scheduler.start()
        self._cleanup_task = asyncio.create_task(
            self._health_loop(), name="application-health"
        )
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(), name="runtime-maintenance")

    async def _update_progress(self, job: DownloadJob) -> None:
        if not self.bot:
            return
        now = time.monotonic()
        terminal = job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
        pct_bucket = int(max(0.0, min(100.0, job.progress_pct)) // 5)
        signature = (job.status.value, job.current_stage, pct_bucket)
        changed = self._progress_signature.get(job.job_id) != signature
        periodic = now - self._progress_last.get(job.job_id, 0.0) >= 15.0
        if not terminal and not changed and not periodic:
            return
        self._progress_last[job.job_id] = now
        self._progress_signature[job.job_id] = signature
        if terminal:
            self._progress_last.pop(job.job_id, None)
            self._progress_signature.pop(job.job_id, None)
        if self.db and not terminal:
            for recipient in await self.db.list_job_subscribers(job.job_id):
                if not recipient["message_id"]:
                    continue
                try:
                    await self.bot.edit_message_text(
                        chat_id=recipient["chat_id"],
                        message_id=recipient["message_id"],
                        text=build_progress_text(job),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(
                                text="❌ Cancel my delivery",
                                callback_data=f"cancel_sub:{recipient['subscriber_id']}",
                            )]]
                        ),
                    )
                except Exception:
                    logger.debug("Recipient progress edit was rejected or unchanged", exc_info=True)
        if terminal and self.db and job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            recipient_statuses = ("waiting", "failed") if job.status == JobStatus.FAILED else ("waiting",)
            for subscriber in await self.db.list_job_subscribers(job.job_id, recipient_statuses):
                status = "cancelled" if job.status == JobStatus.CANCELLED else "failed"
                await self.db.mark_subscriber(subscriber["subscriber_id"], status)
                if subscriber["message_id"]:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=subscriber["chat_id"], message_id=subscriber["message_id"],
                            text=("⏹ <b>Shared download cancelled</b>"
                                  if status == "cancelled" else build_progress_text(job)),
                            reply_markup=(
                                None if status == "cancelled" else build_error_keyboard(job)
                            ),
                        )
                    except Exception:
                        logger.debug("Subscriber terminal edit was rejected", exc_info=True)

    async def _maintenance_loop(self) -> None:
        assert self.maintenance is not None
        while True:
            try:
                await self.maintenance.run()
                await asyncio.sleep(self.settings.maintenance_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maintenance iteration failed")
                await asyncio.sleep(10)

    async def _health_loop(self) -> None:
        while True:
            try:
                await self._write_health()
            except Exception:
                logger.exception("Health heartbeat failed")
            await asyncio.sleep(30)

    async def _write_health(self) -> None:
        database_ok = False
        if self.db:
            try:
                database_ok = await self.db.ping()
            except Exception:
                logger.warning("Health probe could not reach the database")
        payload = json.dumps({
            "timestamp": time.time(),
            "database_ok": database_ok,
            "scheduler_alive": bool(self.scheduler and self.scheduler.is_alive),
            "proxy_alive": bool(self.proxy and self.proxy.is_alive),
            "maintenance_alive": bool(self._maintenance_task and not self._maintenance_task.done()),
            "maintenance_ok": bool(self.maintenance and self.maintenance.last_success and
                time.time() - self.maintenance.last_success < self.settings.maintenance_interval_seconds + 120),
        })
        def write_atomic():
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            pending = self.health_file.with_name(f"health_{os.getpid()}_{time.time_ns()}.tmp")
            pending.write_text(payload, encoding="utf-8")
            try:
                pending.replace(self.health_file)
            except OSError:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass
        await asyncio.to_thread(write_atomic)

    async def start(self) -> None:
        if not self.bot or not self.dp:
            raise RuntimeError("Application is not initialized")
        commands = [
            BotCommand(command="start", description="Show access status"),
            BotCommand(command="help", description="Show usage help"),
            BotCommand(command="settings", description="Downloader preferences"),
            BotCommand(command="queue", description="View active and waiting downloads"),
            BotCommand(command="admin", description="Administration panel"),
            BotCommand(command="allow", description="Authorize a user"),
            BotCommand(command="disallow", description="Revoke a user"),
            BotCommand(command="listusers", description="List registered users"),
        ]
        await self.bot.set_my_commands(commands)
        await self.bot.delete_webhook(drop_pending_updates=True)
        try:
            await self.dp.start_polling(
                self.bot,
                handle_signals=True,
                close_bot_session=False,
                allowed_updates=self.dp.resolve_used_update_types(),
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self.scheduler:
            self.scheduler.pause_new_jobs()
        for task in (self._cleanup_task, self._maintenance_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        handlers = [task for task in self._handler_tasks if task is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if self.scheduler:
            await self.scheduler.stop()
        if self.queue_mgr:
            await self.queue_mgr.prepare_shutdown()
        if self.supervisor:
            await self.supervisor.shutdown()
        if self.proxy:
            await self.proxy.close()
        if self.bot:
            await self.bot.session.close()
        if self.db:
            await self.db.close()


async def main() -> None:
    app = BotApplication()
    try:
        await app.initialize()
        await app.start()
    except KeyboardInterrupt:
        await app.shutdown()
    except Exception:
        logger.exception("Fatal application error")
        await app.shutdown()
        raise


async def setup_bot(settings: Optional[Settings] = None) -> BotApplication:
    """Construct and initialize an application for embedding or smoke tests."""
    app = BotApplication(settings=settings)
    await app.initialize()
    return app


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        sys.exit(1)
