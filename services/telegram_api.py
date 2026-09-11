"""Telegram Bot API abstraction supporting Standard and Local Bot API transports."""

import logging
from pathlib import Path
from typing import Optional
from aiogram import Bot
from aiogram.types import FSInputFile

from core.config import SendMode, Settings, get_settings
from core.exceptions import BotError
from core.models import DownloadJob, MediaFormat

logger = logging.getLogger(__name__)


class DeliverySizeError(BotError):
    error_category = "delivery_size_exceeded"


class TelegramService:
    """Delivers media files to Telegram users using streaming file references."""

    def __init__(self, bot: Bot, settings: Optional[Settings] = None):
        self.bot = bot
        self.settings = settings or get_settings()

    @property
    def max_file_size_bytes(self) -> int:
        """Return maximum deliverable file size in bytes according to API transport mode."""
        if self.settings.use_local_api:
            return self.settings.local_api_max_file_size_mb * 1024 * 1024
        return 50 * 1024 * 1024

    def can_deliver_size(self, size_bytes: Optional[int]) -> tuple[bool, str]:
        """Preflight check whether a file size can be delivered via Telegram."""
        if size_bytes is None:
            return True, "Size unknown; delivery will be checked after download"
        limit = self.max_file_size_bytes
        if size_bytes > limit:
            limit_mb = limit / (1024 * 1024)
            size_mb = size_bytes / (1024 * 1024)
            mode_str = "Local Bot API" if self.settings.use_local_api else "Standard Telegram Bot API"
            return False, f"Estimated size ({size_mb:.1f} MB) exceeds {mode_str} limit ({limit_mb:.0f} MB)."
        return True, "OK"

    def can_deliver_selection(
        self, video: MediaFormat, audio: Optional[MediaFormat] = None
    ) -> tuple[bool, str]:
        """Check estimated delivered bytes, not the temporary disk footprint.

        A lossless merge delivers video plus audio once. The retained source
        streams plus merged output occupy roughly twice that on disk, which
        belongs to disk reservation, not Telegram's per-file limit. Container
        overhead is unknown until muxing; send_media checks the actual result.
        """
        streams = [video] if video.is_muxed or audio is None else [video, audio]
        sizes = [stream.effective_size for stream in streams]
        known_bytes = sum(size for size in sizes if size is not None)
        if any(size is None for size in sizes) and known_bytes <= self.max_file_size_bytes:
            return self.can_deliver_size(None)
        return self.can_deliver_size(known_bytes)

    async def send_media(
        self,
        job: DownloadJob,
        file_path: Path,
        caption: Optional[str] = None
    ) -> None:
        """
        Send the processed media to the user chat.
        Uses streaming FSInputFile to keep memory usage minimal on ~1GB VMs.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Media file missing on disk: {file_path}")

        allowed, reason = self.can_deliver_size(file_path.stat().st_size)
        if not allowed:
            raise DeliverySizeError(reason, user_message=reason)

        input_file = FSInputFile(str(file_path), filename=file_path.name)
        mode = job.send_mode or self.settings.default_send_mode.value

        logger.info(f"Sending file {file_path.name} to chat {job.chat_id} (Mode: {mode})")

        if mode == SendMode.VIDEO.value or mode == "video":
            try:
                msg = await self.bot.send_video(
                    chat_id=job.chat_id,
                    video=input_file,
                    caption=caption,
                    supports_streaming=True
                )
                if msg.video:
                    job.telegram_file_id = msg.video.file_id
                return
            except Exception as e:
                logger.warning(f"Failed sending as video, falling back to document: {e}")
                # Fallback to sending as document/file if Telegram video delivery fails

        # Send as document / file
        msg = await self.bot.send_document(
            chat_id=job.chat_id,
            document=input_file,
            caption=caption
        )
        if msg.document:
            job.telegram_file_id = msg.document.file_id

    async def send_cached(
        self, chat_id: int, file_id: str, send_mode: str, caption: Optional[str] = None
    ) -> None:
        """Send a Telegram-owned file reference without exposing cache provenance."""
        if send_mode == SendMode.VIDEO.value:
            try:
                await self.bot.send_video(
                    chat_id=chat_id, video=file_id, caption=caption, supports_streaming=True
                )
                return
            except Exception:
                logger.info("Cached file is not deliverable as video; trying document")
        await self.bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
