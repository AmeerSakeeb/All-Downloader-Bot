"""High-value Telegram UI flows and callback-contract regression guards."""

from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from bot.callbacks.phase2 import (
    admin_actions, all_formats, collection_item, details, favorites, help_section, home_actions,
    media_info, retry_batch_item, settings_panel, settings_section,
)
from bot.callbacks.operations import operations
from bot.handlers.commands import cmd_help, cmd_start
from core.models import DownloadJob, FavoriteFormatRule, JobStatus, MediaItem, MediaSession, UserSettings
from services.favorites import FavoriteMatchResult
from ui.builders import (
    build_admin_keyboard, build_admin_status_text, build_all_formats_keyboard,
    build_all_formats_text, build_batch_review_keyboard, build_batch_review_text,
    build_collection_keyboard, build_collection_text, build_favorites_keyboard,
    build_favorites_text, build_help_keyboard, build_help_text, build_home_keyboard,
    build_home_text, build_performance_keyboard, build_preferred_keyboard,
    build_preferred_media_text, build_queue_keyboard, build_queue_text,
    build_resource_keyboard, build_settings_keyboard, build_settings_text,
)


class MenuMessage:
    def __init__(self):
        self.edited_text = None
        self.markup = None
        self.chat = SimpleNamespace(id=100)
        self.message_id = 200

    async def edit_text(self, text, **kwargs):
        self.edited_text = text
        self.markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, **kwargs):
        self.markup = kwargs.get("reply_markup")

    async def answer(self, text, **kwargs):
        child = MenuMessage()
        child.edited_text = text
        child.markup = kwargs.get("reply_markup")
        return child


class Callback:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = MenuMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class CommandMessage:
    def __init__(self, user_id: int = 1):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = ""
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))
        return MenuMessage()


def labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def batch_session(settings, video_format) -> MediaSession:
    return MediaSession.with_ttl(
        ttl_seconds=settings.media_session_ttl,
        user_id=1,
        url="https://facebook.com/batch",
        extractor="batch",
        title="Four links",
        session_kind="batch",
        items=[
            MediaItem(
                source_url="https://facebook.com/ready", title="Facebook clip",
                analysis_status="ready", formats=[video_format],
            ),
            MediaItem(
                source_url="https://vt.tiktok.com/blocked", title="TikTok",
                analysis_status="failed", analysis_error_category="site_access_challenge",
            ),
            MediaItem(
                source_url="https://instagram.com/ready", title="Instagram video",
                analysis_status="ready", formats=[video_format],
            ),
            MediaItem(
                source_url="https://youtube.com/auth", title="YouTube",
                analysis_status="failed", analysis_error_category="authentication_required",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_home_new_download_and_shared_help_flow(db, settings):
    governor = SimpleNamespace(settings=settings)
    start_message = CommandMessage()
    await cmd_start(start_message, db, {"is_admin": True}, governor)
    home_text, home_markup = start_message.replies[-1]
    assert home_text == build_home_text(
        active=0, waiting=0, download_target=settings.max_concurrent_downloads,
        mode=settings.resource_mode.value, max_links=settings.max_batch_urls,
    )
    assert "📥 Download from link" in labels(home_markup)
    assert "🛠 Admin Control Center" in labels(home_markup)

    callback = Callback("home:new")
    await home_actions(callback, db, governor)
    assert "Send a link" in callback.message.edited_text
    assert labels(callback.message.markup) == ["❓ How downloads work", "🏠 Home"]

    callback.data = "home:help"
    await home_actions(callback, db, governor)
    assert callback.message.edited_text == build_help_text("main")
    assert labels(callback.message.markup) == labels(build_help_keyboard("main", True))

    callback.data = "help:quick"
    await help_section(callback, db)
    assert "Quick start" in callback.message.edited_text
    assert "↩️ Back to Help" in labels(callback.message.markup)

    callback.data = "help:batch"
    await help_section(callback, db)
    assert "Site blocked server" in callback.message.edited_text

    help_message = CommandMessage()
    await cmd_help(help_message, {"is_admin": True})
    assert help_message.replies[-1][0] == build_help_text("main")
    assert labels(help_message.replies[-1][1]) == labels(build_help_keyboard("main", True))


@pytest.mark.asyncio
async def test_settings_navigation_uses_plain_sections(db, media_session):
    callback = Callback("settings")
    await settings_panel(callback, db)
    assert callback.message.edited_text == build_settings_text(UserSettings(user_id=1))
    top_labels = labels(callback.message.markup)
    assert "🎯 Quality preference" in top_labels
    assert all("Workflow" not in label for label in top_labels)

    for section, heading in (
        ("delivery", "Delivery type"), ("audio", "Audio handling"),
        ("strategy", "Quality preference"), ("display", "Display style"),
    ):
        callback.data = f"uset:{section}"
        await settings_section(callback, db)
        assert heading in callback.message.edited_text
        assert "↩️ Back to settings" in labels(callback.message.markup)

    callback.data = "favorites"
    await favorites(callback, db)
    assert "Preferred Format Rules" in callback.message.edited_text
    assert "They never convert the video" in callback.message.edited_text


@pytest.mark.asyncio
async def test_single_media_format_navigation(db, media_session):
    await db.save_media_session(media_session)
    callback = Callback(f"detail:{media_session.session_id}:{media_session.formats[0].internal_key}")
    await details(callback, db)
    assert "Selected quality" in callback.message.edited_text
    assert "Download this quality" in " ".join(labels(callback.message.markup))

    callback.data = f"all:{media_session.session_id}:0"
    await all_formats(callback, db)
    assert "All source qualities" in callback.message.edited_text
    assert any("Codec: Any" in label for label in labels(callback.message.markup))

    callback.data = f"info:{media_session.session_id}"
    await media_info(callback, db)
    assert "Media details" in callback.message.edited_text
    assert labels(callback.message.markup) == ["↩️ Back to video"]


@pytest.mark.asyncio
async def test_batch_status_review_and_failure_details(db, settings, video_format):
    session = batch_session(settings, video_format)
    await db.save_media_session(session)
    text = build_collection_text(session)
    markup = build_collection_keyboard(session)
    button_labels = labels(markup)
    assert "2 ready" in text and "2 need attention" in text
    assert "✅ 1 · Facebook · Choose quality" in button_labels
    assert "🛡 2 · TikTok · Details" in button_labels
    assert "🔐 4 · YouTube · Details" in button_labels
    assert "⬇️ Prepare 2 ready downloads" in button_labels
    assert all(item.item_id not in " ".join(button_labels) for item in session.items)

    selected = {session.items[0].item_id, session.items[2].item_id}
    review_text = build_batch_review_text(session, selected)
    review_markup = build_batch_review_keyboard(session, selected)
    assert "Selected: 2 of 2 available" in review_text
    assert "☑ 1 · Facebook" in labels(review_markup)
    assert "🔒 2 · TikTok" in labels(review_markup)
    assert callbacks(review_markup)[1].startswith("item:")

    blocked = Callback(f"item:{session.session_id}:{session.items[1].item_id}")
    await collection_item(blocked, db, None, None)
    assert "refused the request from this server" in blocked.message.edited_text
    assert any("Try analysis again" in label for label in labels(blocked.message.markup))

    auth = Callback(f"item:{session.session_id}:{session.items[3].item_id}")
    await collection_item(auth, db, None, None)
    assert "sign-in required" in auth.message.edited_text
    assert not any("Try analysis again" in label for label in labels(auth.message.markup))

    ready = Callback(f"item:{session.session_id}:{session.items[0].item_id}")
    await collection_item(ready, db, None, None)
    assert "Video ready" in ready.message.edited_text


@pytest.mark.asyncio
async def test_batch_retry_runs_only_after_explicit_action(db, settings, video_format, monkeypatch):
    session = batch_session(settings, video_format)
    failed = session.items[1]
    await db.save_media_session(session)
    calls = []

    class Lease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Extractor:
        async def extract(self, url, user_id, **kwargs):
            calls.append((url, user_id, kwargs))
            return MediaSession.with_ttl(
                ttl_seconds=settings.media_session_ttl, user_id=user_id, url=url,
                extractor="test", title="Recovered TikTok", formats=[video_format],
            )

    class Registry:
        async def get_extractor_for_url(self, url):
            return Extractor()

    governor = SimpleNamespace(
        settings=settings,
        acquire_stage=lambda stage: None,
    )

    async def acquire_stage(stage):
        return Lease(), "Ready"

    governor.acquire_stage = acquire_stage
    monkeypatch.setattr("bot.callbacks.phase2.SSRFGuard.validate_url", lambda url: None)

    details_callback = Callback(f"item:{session.session_id}:{failed.item_id}")
    await collection_item(details_callback, db, Registry(), governor)
    assert calls == []

    retry_callback = Callback(f"batchretry:{session.session_id}:{failed.item_id}")
    await retry_batch_item(retry_callback, db, Registry(), governor)
    assert len(calls) == 1
    assert "Video ready" in retry_callback.message.edited_text


@pytest.mark.asyncio
async def test_preferred_rule_delete_requires_confirmation(db):
    from bot.callbacks.phase2 import favorite_delete_confirm, favorite_delete_prompt

    await db.ensure_default_favorite_rules(1)
    rule = FavoriteFormatRule(user_id=1, name="Temporary preferred rule", priority=99)
    await db.save_favorite_rule(rule)
    prompt = Callback(f"fdelete:{rule.rule_id}")
    await favorite_delete_prompt(prompt, db)
    assert await db.get_favorite_rule(rule.rule_id, 1) is not None
    assert "Delete Temporary preferred rule?" in prompt.message.edited_text

    confirm = Callback(f"fdeleteconfirm:{rule.rule_id}")
    await favorite_delete_confirm(confirm, db)
    assert await db.get_favorite_rule(rule.rule_id, 1) is None


def test_queue_progress_and_admin_snapshots_do_not_expose_job_ids():
    jobs = [
        DownloadJob(
            job_id="sensitive-job-id-123", media_session_id="s", user_id=1,
            chat_id=1, video_format_id="v", status=JobStatus.DOWNLOADING_VIDEO,
            progress_pct=42, video_format_snapshot={"resolution_label": "1080p"},
        ),
        DownloadJob(
            job_id="sensitive-job-id-456", media_session_id="s", user_id=1,
            chat_id=1, video_format_id="v", status=JobStatus.QUEUED,
            video_format_snapshot={"resolution_label": "720p"},
        ),
    ]
    text = build_queue_text(jobs, {jobs[1].job_id: 1})
    markup = build_queue_keyboard(jobs)
    assert "1. 1080p" in text and "2. 720p" in text
    assert "❌ Cancel #1 · 1080p" in labels(markup)
    assert "sensitive-job-id" not in " ".join(labels(markup))
    assert jobs[0].job_id in " ".join(callbacks(markup))

    sample = {"cpu": {"load_percent": 10}, "memory": {"available": 4 * 1024**3}}
    stats = {"active": 1, "waiting": 1, "failed": 0, "successful": 2, "cache_hits": 1}
    limits = {"extraction": 1, "download": 1, "merge": 1, "upload": 1}
    assert "Admin Control Center" in build_admin_status_text(sample, stats, limits, "manual", False, 10**9)
    admin_labels = labels(build_admin_keyboard(False))
    assert "⚡ Concurrency & limits" in admin_labels
    assert "🛡 Resource safety mode" in admin_labels
    assert "🔐 Site login sessions" in admin_labels
    assert "📊 System health" in admin_labels
    assert not any("Bot Settings" in label for label in admin_labels)


@pytest.mark.asyncio
async def test_admin_resource_sessions_and_health_have_back_paths(db, settings):
    resource = Callback("admin:resources")
    governor = SimpleNamespace(settings=settings)
    await admin_actions(resource, db, governor, SimpleNamespace(), None)
    assert "Resource safety mode" in resource.message.edited_text
    assert "↩️ Back to admin" in labels(resource.message.markup)

    class Profiles:
        profiles = {}

        async def refresh(self):
            return None

    class Detector:
        async def sample(self):
            return {"cpu": {"load_percent": 5}, "memory": {"available": 2 * 1024**3}}

    class Governor:
        def __init__(self, app_settings):
            self.settings = app_settings
            self.detector = Detector()
            self.file_mgr = SimpleNamespace(get_free_disk_space=lambda: 5 * 1024**3)

        async def adaptive_limits(self):
            return {"extraction": 1, "download": 1, "merge": 1, "upload": 1}

        def current_pressure_reason(self, limits):
            return "Resources currently healthy"

        def disk_safety_bytes(self):
            return 0

    application = SimpleNamespace(
        governor=Governor(settings), scheduler=SimpleNamespace(paused=False, is_alive=True),
        cookie_profiles=Profiles(), started_at=0,
    )
    sessions = Callback("ops:sessions")
    await operations(sessions, db, application)
    assert "Site login sessions" in sessions.message.edited_text
    assert "↩️ Back to admin" in labels(sessions.message.markup)

    health = Callback("ops:status")
    await operations(health, db, application)
    assert "System health" in health.message.edited_text
    assert "↩️ Back to admin" in labels(health.message.markup)


def test_high_value_builder_wording_and_no_action_looking_noops(media_session):
    result = FavoriteMatchResult([media_session.formats[0]], (), 1, 0)
    preferred_text = build_preferred_media_text(media_session, result)
    assert "Video ready" in preferred_text and "Preferred formats" in preferred_text
    assert "FPS ?" not in preferred_text
    all_text = build_all_formats_text(media_session, {})
    assert "All source qualities" in all_text
    assert "Frame rate: Any" in all_text

    markups = [
        build_home_keyboard(is_admin=True, max_links=4),
        build_help_keyboard("main", True), build_settings_keyboard(UserSettings(user_id=1)),
        build_preferred_keyboard(media_session, result),
        build_all_formats_keyboard(media_session, {}),
        build_performance_keyboard({
            "max_batch_urls": 4, "max_concurrent_downloads": 2,
            "max_concurrent_extractions": 2, "max_concurrent_merges": 1,
            "max_concurrent_uploads": 2,
        }),
        build_resource_keyboard("manual"),
    ]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data == "noop":
                    assert "Page" in button.text or "/" in button.text or button.text.startswith("Current:")


def test_every_statically_emitted_callback_prefix_has_a_handler():
    root = Path(__file__).resolve().parents[1]
    emitters = [
        root / "ui" / "builders.py", root / "bot" / "main.py",
        root / "bot" / "handlers" / "commands.py", root / "bot" / "handlers" / "media.py",
        root / "bot" / "callbacks" / "__init__.py", root / "bot" / "callbacks" / "phase2.py",
        root / "bot" / "callbacks" / "operations.py",
    ]
    emitted: set[str] = set()
    for path in emitters:
        source = path.read_text(encoding="utf-8")
        emitted.update(re.findall(r'callback_data=(?:f)?["\']([a-z][a-z0-9_-]*):', source))
        emitted.update(re.findall(r'callback_data=["\']([a-z][a-z0-9_-]*)["\']', source))

    callback_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (
            root / "bot" / "callbacks" / "__init__.py",
            root / "bot" / "callbacks" / "phase2.py",
            root / "bot" / "callbacks" / "operations.py",
        )
    )
    decorator_lines = "\n".join(
        line for line in callback_source.splitlines() if line.startswith("@router.callback_query")
    )
    registered = set(re.findall(r'["\']([a-z][a-z0-9_-]*):', decorator_lines))
    registered.update(re.findall(r'F\.data == ["\']([a-z][a-z0-9_-]*)', decorator_lines))
    registered.update(value.split(":", 1)[0] for value in re.findall(
        r'F\.data == ["\']([a-z][a-z0-9_-]*:[^"\']+)', decorator_lines
    ))
    assert emitted - registered == set()
