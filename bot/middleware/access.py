"""Access control middleware validating Telegram users against the SQLite allowlist."""

import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from core.config import get_settings
from storage.database import Database

logger = logging.getLogger(__name__)


class AccessControlMiddleware(BaseMiddleware):
    """Gates bot access to only allowed/admin users from SQLite database."""

    def __init__(self, db: Database, settings=None):
        super().__init__()
        self.db = db
        self.settings = settings or get_settings()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        # Resolve user details
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user is None:
            logger.warning("Rejected protected update without an attributable user")
            return None

        user_id = user.id

        # Check SQLite db for user status
        user_record = await self.db.get_user(user_id)
        if not user_record:
            if user_id in self.settings.admin_user_ids:
                await self.db.add_or_update_user(user_id, is_admin=True, is_allowed=True)
                logger.info(f"Bootstrapped configured admin user {user_id}")
            else:
                # By default, unknown users are unauthorized
                logger.warning(f"Unauthorized access attempt by unknown user {user_id} (@{user.username})")
                if isinstance(event, Message):
                    await event.reply(
                        "⚠️ <b>Access Denied</b>\n\nYou are not authorized to use this private downloader bot.\n"
                        "Please contact the administrator to request access."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer("Access Denied: You are not authorized.", show_alert=True)
                return None

        # Refetch/validate user state from DB
        user_record = await self.db.get_user(user_id)
        if not user_record or not user_record.get("is_allowed"):
            # Even if configured as admin in settings, the SQLite status is the source of truth
            logger.warning(f"Unauthorized access attempt by disallowed user {user_id}")
            if isinstance(event, Message):
                await event.reply("⚠️ <b>Access Denied</b>\n\nYour account is not authorized to use this bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Access Denied: Account disallowed.", show_alert=True)
            return None

        # Populate context data with user record and settings
        data["user_db"] = user_record
        data["db"] = self.db

        return await handler(event, data)
