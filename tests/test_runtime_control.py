from types import SimpleNamespace

import pytest

from bot.callbacks.phase2 import (
    _filters, admin_actions, all_formats, performance_adjust,
    performance_advanced, performance_reset, prepare_download_all, queue_download_all,
)
from bot.batch_manager import global_batch_manager
from bot.handlers.media import extract_urls_from_text, handle_potential_url
from core.config import Settings
from core.exceptions import ExtractionError
from core.models import DownloadJob, JobStatus, MediaItem, MediaSession
from services.runtime_settings import RuntimeSettingsService
from services.telegram_api import TelegramService
from ui.builders import build_progress_text


class CallbackMessage:
    def __init__(self):
        self.edited_text = None
        self.markup = None

    async def edit_text(self, text, **kwargs):
        self.edited_text = text
        self.markup = kwargs.get("reply_markup")

    async def answer(self, text, **kwargs):
        return StatusMessage()


class StatusMessage(CallbackMessage):
    _next_id = 100

    def __init__(self):
        super().__init__()
        type(self)._next_id += 1
        self.chat = SimpleNamespace(id=50)
        self.message_id = type(self)._next_id


class Callback:
    def __init__(self, user_id, data):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = CallbackMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_browse_all_formats_uses_scoped_draft(db, media_session):
    await db.save_media_session(media_session)
    callback = Callback(1, f"all:{media_session.session_id}:0")
    await all_formats(callback, db)

    assert "All source qualities" in callback.message.edited_text
    assert callback.message.markup is not None
    draft = await db.get_ui_draft(1, f"format_filters:{media_session.session_id}")
    assert draft["session_id"] == media_session.session_id


@pytest.mark.asyncio
async def test_two_media_sessions_keep_separate_format_filter_drafts(db, media_session):
    other = media_session.model_copy(update={"session_id": "other-session"})
    first = await _filters(db, 1, media_session.session_id)
    first["codec"] = ["h265"]
    await db.save_ui_draft(1, f"format_filters:{media_session.session_id}", {
        "kind": "filters", "session_id": media_session.session_id, "filters": first,
    })
    second = await _filters(db, 1, other.session_id)
    second["codec"] = ["h264"]
    await db.save_ui_draft(1, f"format_filters:{other.session_id}", {
        "kind": "filters", "session_id": other.session_id, "filters": second,
    })

    assert (await db.get_ui_draft(
        1, f"format_filters:{media_session.session_id}"
    ))["filters"]["codec"] == ["h265"]
    assert (await db.get_ui_draft(
        1, f"format_filters:{other.session_id}"
    ))["filters"]["codec"] == ["h264"]


def test_four_link_message_produces_four_unique_batch_inputs():
    urls = extract_urls_from_text(
        "https://one.example/v\nhttps://two.example/v "
        "https://three.example/v https://four.example/v https://one.example/v"
    )
    assert len(urls) == 4


@pytest.mark.asyncio
async def test_runtime_override_persists_across_service_recreation(db, settings):
    service = RuntimeSettingsService(db, settings)
    await service.initialize()
    await service.set("max_concurrent_downloads", 3, updated_by=1)

    recreated_settings = Settings(
        bot_token=settings.bot_token,
        data_dir=settings.data_dir,
        jobs_dir=settings.jobs_dir,
        database_path=settings.database_path,
        log_dir=settings.log_dir,
        max_concurrent_downloads=2,
    )
    recreated = RuntimeSettingsService(db, recreated_settings)
    await recreated.initialize()
    assert recreated.get("max_concurrent_downloads") == 3
    assert recreated.is_overridden("max_concurrent_downloads")


@pytest.mark.asyncio
async def test_ordinary_user_cannot_change_global_runtime_setting(db, settings):
    await db.add_or_update_user(2, is_allowed=True)
    service = RuntimeSettingsService(db, settings)
    await service.initialize()
    before = service.get("max_concurrent_downloads")
    callback = Callback(2, "perf:down:1")
    await performance_adjust(callback, db, SimpleNamespace(), service)
    assert service.get("max_concurrent_downloads") == before
    assert callback.answers[-1][1]["show_alert"] is True


class PerformanceGovernor:
    active_counts = {"download": 0, "extraction": 0, "merge": 0, "upload": 0}

    @staticmethod
    async def adaptive_limits():
        return {"download": 2, "extraction": 2, "merge": 1, "upload": 1}

    @staticmethod
    def current_pressure_reason(limits):
        return "Resources currently healthy"


@pytest.mark.asyncio
async def test_admin_performance_renders_updates_and_resets(db, settings):
    service = RuntimeSettingsService(db, settings)
    await service.initialize()
    governor = PerformanceGovernor()
    scheduler = SimpleNamespace(paused=False)

    callback = Callback(1, "admin:performance")
    await admin_actions(callback, db, governor, scheduler, service)
    assert "Concurrency & limits" in callback.message.edited_text

    callback = Callback(1, "perf:down:+1")
    await performance_adjust(callback, db, governor, service)
    assert service.get("max_concurrent_downloads") == 3
    assert "Concurrency & limits" in callback.message.edited_text

    callback = Callback(1, "perfreset:down")
    await performance_reset(callback, db, governor, service)
    assert service.get("max_concurrent_downloads") == service.bootstrap_value(
        "max_concurrent_downloads"
    )
    assert "Concurrency & limits" in callback.message.edited_text


@pytest.mark.asyncio
async def test_admin_advanced_performance_renders_and_updates(db, settings):
    service = RuntimeSettingsService(db, settings)
    await service.initialize()
    callback = Callback(1, "perfadvanced:show")
    await performance_advanced(callback, db, service)
    assert "Advanced limits" in callback.message.edited_text

    before = int(service.get("max_queued_jobs_per_user"))
    callback = Callback(1, "perf:userq:+1")
    await performance_adjust(callback, db, PerformanceGovernor(), service)
    assert service.get("max_queued_jobs_per_user") == before + 1
    assert "Advanced limits" in callback.message.edited_text


@pytest.mark.asyncio
async def test_four_ready_batch_items_can_all_be_queued(
    db, settings, video_format,
):
    assert settings.max_batch_urls == 4
    assert settings.max_queued_jobs_per_user == 6
    parent = MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl, user_id=1,
        url="https://batch.example/post", extractor="batch", title="Four links",
        session_kind="batch",
    )
    await db.save_media_session(parent)
    choices = []
    for index in range(4):
        fmt = video_format.model_copy(update={
            "format_id": f"format-{index}", "internal_key": f"key{index}",
            "is_muxed": True, "requires_separate_audio": False,
        })
        child = MediaSession.with_ttl(
            ttl_seconds=settings.media_session_ttl, user_id=1,
            url=f"https://media{index}.example/video", extractor="test",
            title=f"Video {index + 1}", formats=[fmt],
        )
        await db.save_media_session(child)
        choices.append({
            "session_id": child.session_id, "primary_key": fmt.internal_key,
            "audio_key": None, "media_kind": "video", "title": child.title,
        })
    await db.save_ui_draft(1, f"download-all:{parent.session_id}", {
        "kind": "download_all", "session_id": parent.session_id,
        "choices": choices, "missing": [],
    })
    scheduler = SimpleNamespace(paused=False, wake=lambda: None)
    callback = Callback(1, f"allqueue:{parent.session_id}")
    await queue_download_all(
        callback, db, scheduler, TelegramService(None, settings), settings
    )
    jobs = await db.get_active_jobs_for_user(1)
    assert len(jobs) == 4
    assert all(job.status == JobStatus.QUEUED for job in jobs)
    assert settings.max_concurrent_downloads == 2


def test_progress_never_reports_download_percent_as_overall_completion():
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        status=JobStatus.WAITING_RESOURCES, progress_pct=100,
        current_stage="Waiting for merge: memory safety headroom",
    )
    text = build_progress_text(job)
    assert "Overall: 100" not in text
    assert "safe finalizing slot" in text
    assert "do not need to resend" in text


@pytest.mark.asyncio
async def test_batch_child_failure_does_not_destroy_other_items(
    db, settings, video_format, monkeypatch,
):
    class Lease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FileManager:
        @staticmethod
        def get_free_disk_space():
            return 10**12

    class Governor:
        def __init__(self):
            self.settings = settings
            self.file_mgr = FileManager()

        @staticmethod
        def disk_safety_bytes():
            return 0

        @staticmethod
        async def acquire_stage(stage):
            return Lease(), "Ready"

    class Extractor:
        async def extract(self, url, user_id, **kwargs):
            if "bad.example" in url:
                raise ExtractionError("unavailable")
            return MediaSession.with_ttl(
                ttl_seconds=settings.media_session_ttl, user_id=user_id, url=url,
                extractor="test", title=url.split("//", 1)[1].split(".", 1)[0].title(),
                formats=[video_format],
            )

    class Registry:
        @staticmethod
        async def get_extractor_for_url(url):
            return Extractor()

    class StatusMessage:
        async def edit_text(self, text, **kwargs):
            self.text = text
            self.markup = kwargs.get("reply_markup")

    class Message:
        from_user = SimpleNamespace(id=1)
        text = (
            "https://one.example/v https://bad.example/v "
            "https://three.example/v https://four.example/v"
        )

        def __init__(self):
            self.replies = []

        async def reply(self, text, **kwargs):
            status = StatusMessage()
            status.text = text
            self.replies.append(status)
            return status

    monkeypatch.setattr("bot.handlers.media.SSRFGuard.validate_url", lambda url: None)
    await handle_potential_url(
        Message(), db, Governor(), Registry(), SimpleNamespace(paused=False)
    )
    row = await (
        await db.connection.execute(
            "SELECT session_id FROM media_sessions WHERE extractor='batch'"
        )
    ).fetchone()
    await global_batch_manager.await_completion(row["session_id"])
    batch = await db.get_media_session(row["session_id"])
    assert len(batch.items) == 4
    assert [item.analysis_status for item in batch.items].count("ready") == 3
    assert [item.analysis_status for item in batch.items].count("failed") == 1


@pytest.mark.asyncio
async def test_batch_download_ready_skips_nonready_items(
    db, settings, video_format, monkeypatch,
):
    """Ready items are queued; analyzing/waiting/failed are NOT re-extracted."""
    from bot.callbacks.phase2 import prepare_download_all

    class Lease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FileManager:
        @staticmethod
        def get_free_disk_space():
            return 10**12

    class Governor:
        def __init__(self):
            self.settings = settings
            self.file_mgr = FileManager()

        @staticmethod
        def disk_safety_bytes():
            return 0

        @staticmethod
        async def acquire_stage(stage):
            return Lease(), "Ready"

    class Extractor:
        async def extract(self, url, user_id, **kwargs):
            if "bad.example" in url:
                raise ExtractionError("unavailable")
            return MediaSession.with_ttl(
                ttl_seconds=settings.media_session_ttl, user_id=user_id, url=url,
                extractor="test", title=url.split("//", 1)[1].split(".", 1)[0].title(),
                formats=[video_format],
            )

    class Registry:
        @staticmethod
        async def get_extractor_for_url(url):
            return Extractor()

    parent = MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl, user_id=1,
        url="https://batch.example/post", extractor="batch", title="Four links",
        session_kind="batch",
    )
    await db.save_media_session(parent)

    statuses = ["ready", "ready", "analyzing", "failed"]
    for i, status in enumerate(statuses):
        item = MediaItem(
            kind="video", source_url=f"https://item{i}.example/v",
            title=f"Item {i}", analysis_status=status,
        )
        if status == "ready":
            item.formats = [video_format.model_copy(update={
                "format_id": f"format-{i}", "internal_key": f"key{i}",
                "is_muxed": True, "requires_separate_audio": False,
            })]
        parent.items.append(item)
    await db.save_media_session(parent)

    callback = Callback(1, f"allmedia:{parent.session_id}")
    await prepare_download_all(
        callback, db, Registry(), Governor(),
    )

    assert callback.message.edited_text is not None
    text = callback.message.edited_text
    assert "Still analyzing" in text
    assert "Waiting for analysis" not in text
    assert "Needs attention" in text
    draft = await db.get_ui_draft(1, f"download-all:{parent.session_id}")
    assert draft is not None
    assert len(draft["choices"]) == 2

    for item in parent.items:
        if item.analysis_status == "ready":
            assert any(c["title"] == item.title for c in draft["choices"])
        elif item.analysis_status in ("analyzing", "failed"):
            assert not any(c["title"] == item.title for c in draft["choices"])
