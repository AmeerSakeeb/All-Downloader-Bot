"""Middleware for logging user interactions and callback requests."""

import logging
import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Logs simple metrics about incoming commands, URLs, and callbacks."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        start_time = time.time()
        user_id = "unknown"
        update_type = type(event).__name__
        detail = ""

        if isinstance(event, Message):
            user_id = str(event.from_user.id) if event.from_user else "unknown"
            detail = f"text: {event.text[:50]}" if event.text else "media"
        elif isinstance(event, CallbackQuery):
            user_id = str(event.from_user.id) if event.from_user else "unknown"
            detail = f"callback_data: {event.data}"

        logger.info(f"Incoming update from user {user_id} | Type: {update_type} | Details: {detail}")

        try:
            result = await handler(event, data)
            elapsed = time.time() - start_time
            logger.info(f"Handled update from user {user_id} | Elapsed: {elapsed:.3f}s")
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"Error handling update from user {user_id} after {elapsed:.3f}s: {e}", exc_info=True)
            raise
