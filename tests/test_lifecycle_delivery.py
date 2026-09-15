from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendVideo

from bot.main import BotApplication
from core.models import DownloadJob
from downloads.progress import ProgressTracker
from healthcheck import is_healthy
from services.telegram_api import TelegramService


class Session:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBot:
    def __init__(self, **kwargs):
        self.session = Session()


@pytest.mark.asyncio
async def test_async_app_initialization_and_graceful_shutdown(settings):
    app = BotApplication(settings, bot_factory=FakeBot)
    await app.initialize()
    assert await app.db.ping()
    assert (await app.db.get_user(1))["is_admin"] is True
    assert app.scheduler._service_task is not None
    assert app.extractor_registry is app.scheduler.extractor_registry
    assert app.dp["extractor_registry"] is app.extractor_registry
    await app._write_health()
    assert is_healthy(settings)
    await app.shutdown()
    assert app.bot.session.closed


class DeliveryBot:
    async def send_video(self, **kwargs):
        return SimpleNamespace(video=SimpleNamespace(file_id="video-file-id"))

    async def send_document(self, **kwargs):
        return SimpleNamespace(document=SimpleNamespace(file_id="document-file-id"))


@pytest.mark.asyncio
async def test_telegram_video_delivery_preserves_real_file_id(tmp_path, settings):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"v")
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v", send_mode="video"
    )
    await TelegramService(DeliveryBot(), settings).send_media(job, path)
    assert job.telegram_file_id == "video-file-id"


@pytest.mark.asyncio
async def test_telegram_video_delivery_includes_source_metadata(tmp_path, settings):
    class CapturingBot(DeliveryBot):
        def __init__(self):
            self.calls = []
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.calls.append(kwargs)
            return await super().send_video(**kwargs)

        async def send_document(self, **kwargs):
            self.document_calls += 1
            return await super().send_document(**kwargs)

    path = tmp_path / "vp9.mp4"
    path.write_bytes(b"vp9")
    bot = CapturingBot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        send_mode="video", source_duration=33.505,
        thumbnail_url="https://cdn.example.com/cover.jpg?signature=secret",
        video_format_snapshot={"width": 1920, "height": 1080},
    )
    await TelegramService(bot, settings).send_media(job, path, "caption")
    call = bot.calls[0]
    assert len(bot.calls) == 1 and bot.document_calls == 0
    assert call["cover"] == job.thumbnail_url
    assert call["duration"] == 34
    assert call["width"] == 1920 and call["height"] == 1080
    assert call["supports_streaming"] is True


@pytest.mark.asyncio
async def test_cover_rejection_retries_same_video_without_cover(tmp_path, settings):
    class CoverRejectingBot(DeliveryBot):
        def __init__(self):
            self.video_calls = []
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls.append(kwargs)
            if "cover" in kwargs:
                raise TelegramBadRequest(
                    method=SendVideo(chat_id=1, video="uploaded-video"),
                    message="Bad Request: failed to get HTTP URL content for cover",
                )
            return await super().send_video(**kwargs)

        async def send_document(self, **kwargs):
            self.document_calls += 1
            return await super().send_document(**kwargs)

    path = tmp_path / "v.mp4"
    path.write_bytes(b"v")
    bot = CoverRejectingBot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        send_mode="video", thumbnail_url="https://cdn.example.com/cover.jpg",
    )
    await TelegramService(bot, settings).send_media(job, path)
    assert len(bot.video_calls) == 2
    assert "cover" in bot.video_calls[0] and "cover" not in bot.video_calls[1]
    assert bot.document_calls == 0
    assert job.telegram_file_id == "video-file-id"


@pytest.mark.asyncio
async def test_covered_video_network_error_is_not_retried(tmp_path, settings):
    class NetworkFailingBot(DeliveryBot):
        def __init__(self):
            self.video_calls = 0
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls += 1
            raise TelegramNetworkError(
                method=SendVideo(chat_id=1, video="uploaded-video"),
                message="connection reset",
            )

        async def send_document(self, **kwargs):
            self.document_calls += 1
            return await super().send_document(**kwargs)

    path = tmp_path / "network.mp4"
    path.write_bytes(b"v")
    bot = NetworkFailingBot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        send_mode="video", thumbnail_url="https://cdn.example.com/cover.jpg",
    )
    with pytest.raises(TelegramNetworkError):
        await TelegramService(bot, settings).send_media(job, path)
    assert bot.video_calls == 1
    assert bot.document_calls == 0


@pytest.mark.asyncio
async def test_covered_video_retry_after_is_not_retried(tmp_path, settings):
    class RateLimitedBot(DeliveryBot):
        def __init__(self):
            self.video_calls = 0
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls += 1
            raise TelegramRetryAfter(
                method=SendVideo(chat_id=1, video="uploaded-video"),
                message="Too Many Requests",
                retry_after=30,
            )

        async def send_document(self, **kwargs):
            self.document_calls += 1
            return await super().send_document(**kwargs)

    path = tmp_path / "rate-limited.mp4"
    path.write_bytes(b"v")
    bot = RateLimitedBot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        send_mode="video", thumbnail_url="https://cdn.example.com/cover.jpg",
    )
    with pytest.raises(TelegramRetryAfter):
        await TelegramService(bot, settings).send_media(job, path)
    assert bot.video_calls == 1
    assert bot.document_calls == 0


@pytest.mark.asyncio
async def test_unknown_covered_video_error_is_not_retried(tmp_path, settings):
    class UnknownFailingBot(DeliveryBot):
        def __init__(self):
            self.video_calls = 0
            self.document_calls = 0

        async def send_video(self, **kwargs):
            self.video_calls += 1
            raise RuntimeError("unexpected transport wrapper failure")

        async def send_document(self, **kwargs):
            self.document_calls += 1
            return await super().send_document(**kwargs)

    path = tmp_path / "unknown-error.mp4"
    path.write_bytes(b"v")
    bot = UnknownFailingBot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v",
        send_mode="video", thumbnail_url="https://cdn.example.com/cover.jpg",
    )
    with pytest.raises(RuntimeError):
        await TelegramService(bot, settings).send_media(job, path)
    assert bot.video_calls == 1
    assert bot.document_calls == 0


@pytest.mark.asyncio
async def test_telegram_document_delivery_preserves_real_file_id(tmp_path, settings):
    path = tmp_path / "v.mkv"
    path.write_bytes(b"v")
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="v", send_mode="document"
    )
    await TelegramService(DeliveryBot(), settings).send_media(job, path)
    assert job.telegram_file_id == "document-file-id"


def test_progress_updates_are_throttled(monkeypatch):
    updates = []
    times = iter([1.0, 1.2, 1.3, 4.0])
    monkeypatch.setattr("downloads.progress.time.time", lambda: next(times))
    tracker = ProgressTracker(lambda *values: updates.append(values), throttle_secs=2)
    tracker.parse_line("[download] 1.0% of 10.0MiB at 1.0MiB/s ETA 00:10")
    tracker.parse_line("[download] 2.0% of 10.0MiB at 1.0MiB/s ETA 00:09")
    tracker.parse_line("[download] 3.0% of 10.0MiB at 1.0MiB/s ETA 00:08")
    tracker.parse_line("[download] 4.0% of 10.0MiB at 1.0MiB/s ETA 00:07")
    assert len(updates) == 2
