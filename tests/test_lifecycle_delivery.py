from types import SimpleNamespace

import pytest

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
