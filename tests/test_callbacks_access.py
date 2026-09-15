import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, User

from bot.callbacks import callback_format, callback_page
from bot.middleware.access import AccessControlMiddleware
from core.models import JobStatus
from services.telegram_api import TelegramService


class CallbackMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=50)
        self.message_id = 60
        self.edited_text = None
        self.markup = None

    async def edit_text(self, text, **kwargs):
        self.edited_text = text
        self.markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, **kwargs):
        self.markup = kwargs.get("reply_markup")


class Callback:
    def __init__(self, user_id, data):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = CallbackMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class Scheduler:
    def __init__(self):
        self.woken = False
        self.paused = False

    def wake(self):
        self.woken = True


@pytest.mark.asyncio
async def test_callback_ownership(db, media_session):
    await db.add_or_update_user(2, is_allowed=True)
    await db.save_media_session(media_session)
    callback = Callback(2, f"page:{media_session.session_id}:0")
    await callback_page(callback, db)
    assert callback.answers[-1][1]["show_alert"] is True
    assert callback.message.markup is None


@pytest.mark.asyncio
async def test_callback_expiry(db, media_session):
    expired = media_session.model_copy(update={"expires_at": time.time() - 1})
    await db.save_media_session(expired)
    callback = Callback(1, f"page:{expired.session_id}:0")
    await callback_page(callback, db)
    assert "stale" in callback.answers[-1][0]


@pytest.mark.asyncio
async def test_page_navigation_uses_saved_session(db, media_session):
    await db.save_media_session(media_session)
    callback = Callback(1, f"page:{media_session.session_id}:0")
    await callback_page(callback, db)
    assert callback.message.markup is not None


@pytest.mark.asyncio
async def test_exact_format_selection_creates_job(
    db, media_session, video_format, settings
):
    await db.save_media_session(media_session)
    scheduler = Scheduler()
    callback = Callback(1, f"fmt:{media_session.session_id}:{video_format.internal_key}")
    await callback_format(callback, db, scheduler, settings, TelegramService(None, settings))
    queued = await db.get_jobs_by_status(JobStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].video_format_id == video_format.format_id
    assert scheduler.woken


@pytest.mark.asyncio
async def test_selection_rejects_transport_and_queue_limits(
    db, media_session, video_format, settings
):
    scheduler = Scheduler()
    service = TelegramService(None, settings)
    oversized = video_format.model_copy(update={"filesize": 60 * 1024 * 1024})
    too_large_session = media_session.model_copy(
        update={"formats": [oversized, *media_session.formats[1:]]}
    )
    await db.save_media_session(too_large_session)
    callback = Callback(1, f"fmt:{too_large_session.session_id}:{oversized.internal_key}")
    await callback_format(callback, db, scheduler, settings, service)
    assert "exceeds" in callback.answers[-1][0]
    assert "too large to send" in callback.message.edited_text
    labels = [
        button.text for row in callback.message.markup.inline_keyboard for button in row
    ]
    assert "🎞 Choose another quality" in labels and "↩️ Back" in labels
    assert await db.count_total_active_jobs() == 0

    await db.save_media_session(media_session)
    first = Callback(1, f"fmt:{media_session.session_id}:{video_format.internal_key}")
    limited = settings.model_copy(update={"max_queued_jobs_per_user": 1})
    await callback_format(first, db, scheduler, limited, service)
    other_format = video_format.model_copy(update={"format_id": "138", "internal_key": "diffkey123"})
    other_session = media_session.model_copy(
        update={"session_id": "session_diff_queue_limit", "formats": [other_format, *media_session.formats[1:]]}
    )
    await db.save_media_session(other_session)
    second = Callback(1, f"fmt:{other_session.session_id}:{other_format.internal_key}")
    await callback_format(second, db, scheduler, limited, service)
    assert second.answers[-1][0] == "Preparing download…"
    assert "maximum number" in second.message.edited_text
    assert await db.count_total_active_jobs() == 1


@pytest.mark.asyncio
async def test_allowlist_runs_before_handler(db, settings, monkeypatch):
    middleware = AccessControlMiddleware(db, settings)
    event = Message(
        message_id=1,
        date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        chat=Chat(id=99, type="private"),
        from_user=User(id=99, is_bot=False, first_name="No"),
        text="https://example.com",
    )
    reply = AsyncMock()
    monkeypatch.setattr(Message, "reply", reply)
    handler = AsyncMock()
    await middleware(handler, event, {})
    handler.assert_not_awaited()
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_authorized_handler_receives_user_record(db, settings):
    middleware = AccessControlMiddleware(db, settings)
    event = Message(
        message_id=1,
        date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        text="/start",
    )
    seen = {}

    async def handler(event, data):
        seen.update(data)

    await middleware(handler, event, {})
    assert seen["user_db"]["is_admin"] is True
