"""Telegram Bot API abstraction supporting Standard and Local Bot API transports."""

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from core.config import SendMode, Settings, get_settings
from core.exceptions import BotError, ErrorCategory
from core.models import DownloadJob, MediaFormat, whole_duration_seconds
from services.telegram_capabilities import TelegramCapabilities

logger = logging.getLogger(__name__)


class DeliverySizeError(BotError):
    error_category = ErrorCategory.DELIVERY_SIZE_EXCEEDED.value

    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message, user_message=user_message)
        if "temporarily unavailable" in message.lower():
            self.error_category = ErrorCategory.TELEGRAM_DELIVERY_UNAVAILABLE.value


class TelegramService:
    """Delivers media files to Telegram users using streaming file references."""

    def __init__(
        self,
        bot: Bot,
        settings: Optional[Settings] = None,
        capabilities: TelegramCapabilities | None = None,
    ):
        self.bot = bot
        self.settings = settings or get_settings()
        self.capabilities = capabilities or TelegramCapabilities.from_settings(self.settings)

    @property
    def max_file_size_bytes(self) -> int:
        """Return maximum deliverable file size in bytes according to API transport mode."""
        return self.capabilities.max_upload_bytes

    def can_deliver_size(self, size_bytes: Optional[int]) -> tuple[bool, str]:
        """Preflight check whether a file size can be delivered via Telegram."""
        return self.capabilities.can_upload(size_bytes)

    async def refresh_capabilities(self) -> bool:
        """Verify readiness with a real Telegram method on the active transport."""

        async def telegram_probe(_base_url: str) -> bool:
            await self.bot.get_me()
            return True

        return await self.capabilities.refresh(
            self.settings.local_api_base_url, telegram_probe,
        )

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

        mode = job.send_mode or self.settings.default_send_mode.value

        logger.info(f"Sending file {file_path.name} to chat {job.chat_id} (Mode: {mode})")

        if mode == SendMode.VIDEO.value or mode == "video":
            snapshot = job.video_format_snapshot or {}
            video_metadata = {
                "supports_streaming": True,
                **self._positive_dimension("width", snapshot.get("width")),
                **self._positive_dimension("height", snapshot.get("height")),
            }
            duration = whole_duration_seconds(job.source_duration)
            if duration is not None:
                video_metadata["duration"] = duration
            cover = self._safe_cover_url(job.thumbnail_url)
            if cover:
                video_metadata["cover"] = cover
            try:
                msg = await self.bot.send_video(
                    chat_id=job.chat_id,
                    video=FSInputFile(str(file_path), filename=file_path.name),
                    caption=caption,
                    **video_metadata,
                )
                if msg.video:
                    job.telegram_file_id = msg.video.file_id
                return
            except TelegramBadRequest as error:
                if cover and self._is_cover_bad_request(error):
                    logger.info("Telegram rejected video delivery metadata; retrying without source cover")
                    video_metadata.pop("cover", None)
                    try:
                        msg = await self.bot.send_video(
                            chat_id=job.chat_id,
                            video=FSInputFile(str(file_path), filename=file_path.name),
                            caption=caption,
                            **video_metadata,
                        )
                        if msg.video:
                            job.telegram_file_id = msg.video.file_id
                        return
                    except TelegramBadRequest:
                        pass
                logger.warning("Telegram video delivery failed; falling back to document")
                # Preserve the existing safe document fallback for unusual codecs.

        # Send as document / file
        msg = await self.bot.send_document(
            chat_id=job.chat_id,
            document=FSInputFile(str(file_path), filename=file_path.name),
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

    @staticmethod
    def _positive_dimension(name: str, value) -> dict[str, int]:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return {name: value}
        return {}

    @staticmethod
    def _safe_cover_url(value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return value

    @staticmethod
    def _is_cover_bad_request(error: TelegramBadRequest) -> bool:
        """Recognize only API rejections plausibly caused by a remote cover."""
        message = str(getattr(error, "message", "") or "").lower()
        if any(phrase in message for phrase in (
            "failed to get http url content",
            "failed to fetch http url content",
            "wrong http url specified",
            "wrong file identifier/http url specified",
        )):
            return True
        if not any(subject in message for subject in ("cover", "thumbnail")):
            return False
        return any(reason in message for reason in (
            "invalid", "wrong", "rejected", "failed", "unavailable", "expired",
            "unsupported", "unacceptable", "not found", "cannot", "can't",
        ))
