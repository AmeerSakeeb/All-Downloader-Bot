"""Telegram Bot Application."""

from bot.handlers.commands import router as commands_router
from bot.handlers.media import router as media_router
from bot.main import main, setup_bot

__all__ = [
    "commands_router",
    "media_router",
    "main",
    "setup_bot",
]
