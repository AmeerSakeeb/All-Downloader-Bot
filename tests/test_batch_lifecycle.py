import asyncio
from types import SimpleNamespace

import pytest

import bot.callbacks as callbacks_module
import bot.handlers.media as media_module
from bot.batch_manager import BatchAnalysisManager
from bot.callbacks.phase2 import (
    _build_review_keyboard,
    item_toggle,
    process_selected,
)
from core.models import MediaItem, MediaSession


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=1)
        self.text = text
        self.edited_text = None
        self.markup = None
        self.answers = []
        self.replies = []

    async def reply(self, text, **kwargs):
        reply = FakeMessage()
        reply.edited_text = text
        reply.markup = kwargs.get("reply_markup")
        self.replies.append(reply)
        return reply

    async def edit_text(self, text, **kwargs):
        self.edited_text = text
        self.markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, **kwargs):
        self.markup = kwargs.get("reply_markup")

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return FakeMessage()


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.from_user = SimpleNamespace(id=1)
        self.data = data
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FileManager:
    @staticmethod
    def get_free_disk_space():
        return 10**12


def button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callback_data(markup) -> list[str | None]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_batch_handler_returns_after_scheduling(
    db, settings, monkeypatch,
):
    manager = BatchAnalysisManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def held_analysis(**kwargs):
        started.set()
        await release.wait()

    governor = SimpleNamespace(
        settings=settings,
        file_mgr=FileManager(),
        disk_safety_bytes=lambda: 0,
    )
    message = FakeMessage(
        " ".join(f"https://item{i}.example/video" for i in range(4))
    )
    monkeypatch.setattr(media_module, "global_batch_manager", manager)
    monkeypatch.setattr(media_module, "_analyze_batch", held_analysis)
    monkeypatch.setattr(media_module.SSRFGuard, "validate_url", lambda url: None)

    await asyncio.wait_for(
        media_module.handle_potential_url(
            message,
            db,
            governor,
            SimpleNamespace(),
            SimpleNamespace(paused=False),
        ),
        timeout=0.5,
    )

    await asyncio.wait_for(started.wait(), timeout=0.5)
    row = await (
        await db.connection.execute(
            "SELECT session_id FROM media_sessions WHERE extractor='batch'"
        )
    ).fetchone()
    assert row is not None
    assert manager.has_task(row["session_id"])
    release.set()
    await manager.await_completion(row["session_id"])


@pytest.mark.asyncio
async def test_async_cancel_awaits_task_and_preserves_marker_until_done():
    manager = BatchAnalysisManager()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def long_running():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert manager.is_cancelled("batch-1")
            cancelling.set()
            await finish_cleanup.wait()

    manager.start("batch-1", long_running())
    await started.wait()
    cancel_call = asyncio.create_task(manager.cancel("batch-1"))
    await cancelling.wait()

    assert manager.is_cancelled("batch-1")
    assert manager.has_task("batch-1")
    assert not cancel_call.done()

    finish_cleanup.set()
    await cancel_call
    assert not manager.is_cancelled("batch-1")
    assert not manager.has_task("batch-1")
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_completed_batch_task_is_automatically_removed():
    manager = BatchAnalysisManager()

    async def short_task():
        await asyncio.sleep(0)

    task = manager.start("batch-2", short_task())
    await task
    await asyncio.sleep(0)

    assert not manager.has_task("batch-2")
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_cancel_batch_confirm_reaps_task_deletes_session_and_drafts(
    db, settings, monkeypatch,
):
    manager = BatchAnalysisManager()
    started = asyncio.Event()

    async def long_running():
        started.set()
        await asyncio.Event().wait()

    session = MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl,
        user_id=1,
        url="https://batch.example/list",
        extractor="batch",
        title="Batch",
        session_kind="batch",
    )
    await db.save_media_session(session)
    for key, value in (
        (f"cancel-confirm:{session.session_id}", {"confirmed": False}),
        (f"batch-view:{session.session_id}", {"view": "status"}),
        (f"items:{session.session_id}", {"selected": []}),
    ):
        await db.save_ui_draft(1, key, value)

    manager.start(session.session_id, long_running())
    await started.wait()
    monkeypatch.setattr(callbacks_module, "global_batch_manager", manager)
    callback = FakeCallback(f"cancel_batch_confirm:{session.session_id}")

    await callbacks_module.callback_cancel_batch_confirm(callback, db)

    assert manager.active_count == 0
    assert await db.get_media_session(session.session_id) is None
    assert "Batch analysis stopped" in callback.message.edited_text
    assert callback.answers[-1][0] == "Batch analysis stopped"
    assert await db.get_ui_draft(1, f"cancel-confirm:{session.session_id}") is None
    assert await db.get_ui_draft(1, f"batch-view:{session.session_id}") is None
    assert await db.get_ui_draft(1, f"items:{session.session_id}") is None


@pytest.mark.asyncio
async def test_batch_review_selection_checkmarks_and_nonready_lock(
    db, settings, video_format,
):
    ready = MediaItem(
        source_url="https://ready.example/video",
        title="Ready",
        analysis_status="ready",
        formats=[video_format],
    )
    waiting = MediaItem(
        source_url="https://waiting.example/video",
        title="Waiting",
        analysis_status="waiting",
    )
    session = MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl,
        user_id=1,
        url="https://batch.example/list",
        extractor="batch",
        title="Batch",
        session_kind="batch",
        items=[ready, waiting],
    )
    await db.save_media_session(session)
    await db.save_ui_draft(1, f"items:{session.session_id}", {
        "kind": "items", "session_id": session.session_id, "selected": [],
    })

    initial = _build_review_keyboard(session, set())
    assert "☐ 1 · Ready" in button_texts(initial)
    assert "🔒 2 · Waiting" in button_texts(initial)
    assert ready.item_id[:6] not in " ".join(button_texts(initial))

    callback = FakeCallback(f"itoggle:{session.session_id}:{ready.item_id}:0")
    await item_toggle(callback, db)
    assert "☑ 1 · Ready" in button_texts(callback.message.markup)
    assert f"iprocess:{session.session_id}" in callback_data(callback.message.markup)

    callback = FakeCallback(f"itoggle:{session.session_id}:{ready.item_id}:0")
    await item_toggle(callback, db)
    assert "☐ 1 · Ready" in button_texts(callback.message.markup)

    callback = FakeCallback(f"itoggle:{session.session_id}:{waiting.item_id}:0")
    await item_toggle(callback, db)
    draft = await db.get_ui_draft(1, f"items:{session.session_id}")
    assert waiting.item_id not in draft["selected"]
    assert callback.answers[-1][0] == "This item is waiting for analysis."


@pytest.mark.asyncio
async def test_process_selected_uses_persisted_ready_metadata_without_extraction(
    db, settings, video_format,
):
    ready = MediaItem(
        source_url="https://ready.example/video",
        title="Ready",
        analysis_status="ready",
        extractor_id="persisted-id",
        source_extractor="test",
        formats=[video_format],
    )
    session = MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl,
        user_id=1,
        url="https://batch.example/list",
        extractor="batch",
        title="Batch",
        session_kind="batch",
        items=[ready],
    )
    await db.save_media_session(session)
    await db.save_ui_draft(1, f"items:{session.session_id}", {
        "kind": "items",
        "session_id": session.session_id,
        "selected": [ready.item_id],
    })

    class Registry:
        @staticmethod
        async def get_extractor_for_url(url):
            raise AssertionError("batch review must not extract again")

    callback = FakeCallback(f"iprocess:{session.session_id}")
    await process_selected(callback, db, Registry(), SimpleNamespace())

    rows = await (
        await db.connection.execute(
            "SELECT session_id FROM media_sessions WHERE session_id<>?",
            (session.session_id,),
        )
    ).fetchall()
    assert len(rows) == 1
    child = await db.get_media_session(rows[0]["session_id"])
    assert child.media_id == "persisted-id"
    assert child.formats[0].format_id == video_format.format_id
