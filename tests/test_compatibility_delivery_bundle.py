import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetMe
from aiogram.types import FSInputFile

from core.exceptions import (
    DRMProtectedError, ErrorCategory, ExtractorCompatibilityError, NoFormatsError,
)
from core.models import DownloadJob, MediaFormat
from downloads.process_supervisor import ProcessResult
from extractors.format_manager import normalize_format_inventory
from extractors.ytdlp_extractor import YtDlpExtractor
from services.compatibility import CompatibilityOverride, CompatibilityOverrideRegistry
from services.site_policy import SiteBehavior, SitePolicy, SitePolicyRegistry
from services.telegram_api import DeliverySizeError, TelegramService
from services.telegram_capabilities import STANDARD_UPLOAD_LIMIT_BYTES, TelegramCapabilities
from ui.builders import (
    build_admin_status_text, build_delivery_limit_text, build_error_text,
    build_format_details_text,
)


def test_site_policy_uses_exact_domains_and_generic_default():
    registry = SitePolicyRegistry()
    assert registry.resolve("https://vm.tiktok.com/abc").canonical_name == "TikTok"
    assert registry.resolve("https://vt.tiktok.com/abc").initial_impersonation is True
    assert registry.resolve("https://creator.tiktok.com/abc").policy_id == "generic"
    assert registry.resolve("https://tiktok.com.attacker.example/abc").policy_id == "generic"
    assert registry.resolve("https://new-supported.example/media").policy_id == "generic"


def test_format_preservation_keeps_distinct_protocols_codecs_and_audio():
    raw = [
        {"format_id": "a", "vcodec": "h264", "acodec": "aac", "height": 1080, "protocol": "https"},
        {"format_id": "b", "vcodec": "h264", "acodec": "none", "height": 1080, "protocol": "m3u8_native"},
        {"format_id": "c", "vcodec": "vp9", "acodec": "none", "height": 1080, "protocol": "http_dash_segments"},
        {"format_id": "d", "vcodec": "h264", "acodec": "none", "height": 720, "protocol": "https"},
        {"format_id": "e", "vcodec": "none", "acodec": "opus", "language": "en"},
    ]
    formats = normalize_format_inventory(raw)
    assert [item.format_id for item in formats] == ["a", "b", "c", "d", "e"]
    assert {item.protocol for item in formats} >= {"https", "m3u8_native", "http_dash_segments"}


def test_unknown_metadata_and_unusual_format_remain_selectable():
    item = normalize_format_inventory([{
        "format_id": "future", "vcodec": "future-vcodec", "acodec": "none",
        "fps": None, "filesize": None, "width": None, "height": None,
        "ext": "future-container", "protocol": "unusual_manifest",
        "dynamic_range": "HDR-X", "quality": 7, "source_preference": -1,
    }])[0]
    assert item.format_id == "future"
    assert item.fps is None and item.filesize is None and item.width is None
    assert item.vcodec_raw == "future-vcodec"
    assert item.ext == "future-container" and item.dynamic_range == "HDR-X"
    assert item.execution_snapshot()["source_preference"] == -1.0

    incomplete = normalize_format_inventory([{
        "format_id": "direct-unknown-codecs", "url": "https://cdn.example/media.bin",
        "ext": "odd-container", "vcodec": None, "acodec": None,
    }])[0]
    assert incomplete.is_video is True
    assert incomplete.vcodec_raw is None


def test_normalization_discards_only_unusable_or_explicit_drm_records():
    formats = normalize_format_inventory([
        "internal-object",
        {"format_id": "", "vcodec": "h264"},
        {"format_id": "drm", "vcodec": "h264", "has_drm": True},
        {"format_id": "ok", "vcodec": "h264", "acodec": "none"},
    ])
    assert [item.format_id for item in formats] == ["ok"]


def test_direct_extractor_metadata_outside_formats_is_retained(settings):
    extractor = YtDlpExtractor(settings=settings, proxy_url="http://proxy")
    session = extractor._build_session(
        {
            "id": "direct-id", "title": "Direct", "format_id": "source-direct",
            "url": "https://cdn.example/media.mp4", "ext": "mp4",
            "vcodec": "h264", "acodec": "aac",
        },
        "https://example.com/watch", 1, settings, impersonated=False,
    )
    assert [item.format_id for item in session.formats] == ["source-direct"]


def test_no_formats_and_all_drm_are_distinguished(settings):
    extractor = YtDlpExtractor(settings=settings, proxy_url="http://proxy")
    with pytest.raises(NoFormatsError):
        extractor._build_session({}, "https://example.com/empty", 1, settings, impersonated=False)
    with pytest.raises(DRMProtectedError):
        extractor._build_session(
            {"formats": [{"format_id": "x", "vcodec": "h264", "has_drm": True}]},
            "https://example.com/drm", 1, settings, impersonated=False,
        )


@pytest.mark.asyncio
async def test_generic_domain_still_uses_generic_ytdlp_fallback(monkeypatch, settings):
    class Supervisor:
        calls = 0

        async def run(self, command, **kwargs):
            self.calls += 1
            return ProcessResult(0, json.dumps({
                "formats": [{"format_id": "generic", "vcodec": "h264", "acodec": "aac"}],
            }).encode(), b"")

    monkeypatch.setattr("extractors.ytdlp_extractor.SSRFGuard.validate_url", lambda url: [])
    monkeypatch.setattr(YtDlpExtractor, "_validate_extracted_urls", classmethod(lambda cls, metadata: _done()))
    supervisor = Supervisor()
    session = await YtDlpExtractor(
        settings=settings, supervisor=supervisor, proxy_url="http://proxy",
    ).extract("https://new-supported.example/watch/1", 1)
    assert supervisor.calls == 1
    assert [item.format_id for item in session.formats] == ["generic"]


async def _done():
    return None


@pytest.mark.asyncio
async def test_version_gated_compatibility_override_repairs_once(monkeypatch, settings):
    repair_calls = []

    def repair(metadata):
        repair_calls.append(metadata)
        return {**metadata, "formats": metadata["broken_streams"]}

    override = CompatibilityOverride(
        override_id="fixture-repair", site_policy_id="fixture",
        reason="Fixture parser changed", tested_ytdlp_version="2026.8.19",
        upstream_reference="fixture-upstream-123", enabled_from="2026.8.1",
        enabled_before="2026.9.1", repair_metadata=repair,
    )
    policy = SitePolicy(
        policy_id="fixture", canonical_name="Fixture",
        exact_domains=frozenset({"compat.example"}),
        behavior=SiteBehavior.COMPATIBILITY_OVERRIDE,
        compatibility_override="fixture-repair",
    )

    class Supervisor:
        calls = 0

        async def run(self, command, **kwargs):
            self.calls += 1
            payload = {"broken_streams": [
                {"format_id": "p", "vcodec": "h264", "acodec": "aac"},
                {"format_id": "h", "vcodec": "vp9", "acodec": "none", "protocol": "m3u8_native"},
            ]}
            return ProcessResult(0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr("extractors.ytdlp_extractor.SSRFGuard.validate_url", lambda url: [])
    monkeypatch.setattr(YtDlpExtractor, "_validate_extracted_urls", classmethod(lambda cls, metadata: _done()))
    active = CompatibilityOverrideRegistry((override,), ytdlp_version="2026.8.19")
    extractor = YtDlpExtractor(
        settings=settings, supervisor=Supervisor(), proxy_url="http://proxy",
        site_policies=SitePolicyRegistry((policy,)), compatibility_overrides=active,
    )
    session = await extractor.extract("https://compat.example/watch", 1)
    assert [item.format_id for item in session.formats] == ["p", "h"]
    assert len(repair_calls) == 1 and active.enabled_count == 1

    future = CompatibilityOverrideRegistry((override,), ytdlp_version="2026.9.1")
    assert future.active_for("fixture-repair") is None
    assert future.enabled_count == 0
    upstream = {"formats": [{"format_id": "upstream-fixed"}]}
    assert future.apply("fixture-repair", upstream, site_policy_id="fixture") is upstream


@pytest.mark.asyncio
async def test_parser_failure_has_no_pointless_retry(monkeypatch, settings):
    class Supervisor:
        calls = 0

        async def run(self, command, **kwargs):
            self.calls += 1
            return ProcessResult(1, b"", b"ERROR: Unable to extract expected key; parser error")

    monkeypatch.setattr("extractors.ytdlp_extractor.SSRFGuard.validate_url", lambda url: [])
    supervisor = Supervisor()
    with pytest.raises(ExtractorCompatibilityError):
        await YtDlpExtractor(
            settings=settings, supervisor=supervisor, proxy_url="http://proxy",
        ).extract("https://example.com/changed", 1)
    assert supervisor.calls == 1


def test_failure_taxonomy_distinguishes_network_geo_and_compatibility():
    classify = YtDlpExtractor._classify_failure
    assert classify("TLS handshake timed out") == "network_failure"
    assert classify("This video is not available in your country") == "geo_restricted"
    assert classify("Unable to extract expected key") == "extractor_compatibility_failure"
    assert classify("HTTP Error 403: Forbidden") == "site_access_challenge"


def test_telegram_capability_preflight_matrix(settings):
    standard = TelegramCapabilities.from_settings(settings)
    assert standard.max_upload_bytes == STANDARD_UPLOAD_LIMIT_BYTES
    assert standard.can_upload(320 * 1024**2)[0] is False
    assert standard.can_upload(None)[0] is True

    local_settings = settings.model_copy(update={
        "use_local_api": True, "local_api_max_file_size_mb": 2000,
    })
    local = TelegramCapabilities.from_settings(local_settings)
    local.endpoint_available = True
    local.max_upload_bytes = local.configured_local_max_bytes
    assert local.can_upload(320 * 1024**2)[0] is True
    assert local.can_upload(2001 * 1024**2)[0] is False
    local.endpoint_available = False
    allowed, reason = local.can_upload(320 * 1024**2)
    assert allowed is False and "temporarily unavailable" in reason


@pytest.mark.asyncio
async def test_local_api_health_controls_capability(settings):
    local_settings = settings.model_copy(update={"use_local_api": True})
    capabilities = TelegramCapabilities.from_settings(local_settings)

    async def available(url):
        assert url == local_settings.local_api_base_url
        return True

    async def unavailable(url):
        return False

    assert await capabilities.refresh(local_settings.local_api_base_url, available) is True
    assert capabilities.endpoint_available is True
    assert capabilities.max_upload_bytes == capabilities.configured_local_max_bytes
    assert await capabilities.refresh(local_settings.local_api_base_url, unavailable) is False
    assert capabilities.endpoint_available is False
    assert capabilities.max_upload_bytes == 0


@pytest.mark.asyncio
async def test_local_readiness_uses_successful_telegram_get_me(settings):
    class Bot:
        calls = 0

        async def get_me(self):
            self.calls += 1
            return SimpleNamespace(id=123, is_bot=True)

    local_settings = settings.model_copy(update={
        "use_local_api": True, "local_api_max_file_size_mb": 2000,
    })
    bot = Bot()
    service = TelegramService(bot, local_settings)
    assert await service.refresh_capabilities() is True
    assert bot.calls == 1
    assert service.capabilities.endpoint_available is True
    assert service.max_file_size_bytes == 2000 * 1024**2


@pytest.mark.asyncio
async def test_local_telegram_network_error_disables_large_files(settings):
    class Bot:
        async def get_me(self):
            raise TelegramNetworkError(method=GetMe(), message="connection failed")

    local_settings = settings.model_copy(update={"use_local_api": True})
    service = TelegramService(Bot(), local_settings)
    assert await service.refresh_capabilities() is False
    assert service.capabilities.endpoint_available is False
    assert service.max_file_size_bytes == 0


@pytest.mark.asyncio
async def test_local_http_404_does_not_qualify_as_telegram_readiness(settings):
    class UnrelatedHttpService:
        async def get_me(self):
            raise TelegramBadRequest(method=GetMe(), message="Not Found")

    local_settings = settings.model_copy(update={"use_local_api": True})
    service = TelegramService(UnrelatedHttpService(), local_settings)
    assert await service.refresh_capabilities() is False
    assert service.capabilities.endpoint_available is False
    assert service.max_file_size_bytes == 0

    without_telegram_probe = TelegramCapabilities.from_settings(local_settings)
    assert await without_telegram_probe.refresh(local_settings.local_api_base_url) is False


@pytest.mark.asyncio
async def test_standard_api_capability_does_not_require_readiness_probe(settings):
    class Bot:
        calls = 0

        async def get_me(self):
            self.calls += 1
            raise AssertionError("Standard capability must not probe readiness")

    bot = Bot()
    service = TelegramService(bot, settings)
    assert await service.refresh_capabilities() is True
    assert bot.calls == 0
    assert service.capabilities.endpoint_available is True
    assert service.max_file_size_bytes == STANDARD_UPLOAD_LIMIT_BYTES


@pytest.mark.asyncio
async def test_actual_output_over_limit_never_calls_telegram(tmp_path, settings):
    class Bot:
        calls = 0

        async def send_document(self, **kwargs):
            self.calls += 1

    path = tmp_path / "large.bin"
    with path.open("wb") as handle:
        handle.truncate(STANDARD_UPLOAD_LIMIT_BYTES + 1)
    bot = Bot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="x",
        send_mode="document",
    )
    with pytest.raises(DeliverySizeError):
        await TelegramService(bot, settings).send_media(job, path)
    assert bot.calls == 0


def test_unavailable_local_transport_records_distinct_delivery_category(settings):
    local_settings = settings.model_copy(update={"use_local_api": True})
    service = TelegramService(None, local_settings)
    allowed, reason = service.can_deliver_size(320 * 1024**2)
    assert allowed is False
    error = DeliverySizeError(reason, user_message=reason)
    assert error.error_category == ErrorCategory.TELEGRAM_DELIVERY_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_delivery_passes_streaming_path_object_not_file_bytes(tmp_path, settings):
    class Bot:
        received = None

        async def send_document(self, **kwargs):
            self.received = kwargs["document"]
            return SimpleNamespace(document=SimpleNamespace(file_id="id"))

    path = tmp_path / "media.bin"
    path.write_bytes(b"small fixture")
    bot = Bot()
    job = DownloadJob(
        media_session_id="s", user_id=1, chat_id=1, video_format_id="x",
        send_mode="document",
    )
    await TelegramService(bot, settings).send_media(job, path)
    assert isinstance(bot.received, FSInputFile)
    assert not isinstance(bot.received, (bytes, bytearray))


def test_error_and_admin_ui_use_safe_capability_language():
    for category, expected in (
        ("extractor_compatibility_failure", "website changed"),
        ("no_formats", "No downloadable source qualities"),
        ("network_failure", "source connection failed"),
        ("geo_restricted", "bot's region"),
    ):
        text = build_error_text(category)
        assert expected.lower() in text.lower()
        assert "traceback" not in text.lower()

    local = TelegramCapabilities(
        mode="local", local_api_enabled=True, endpoint_available=False,
        max_upload_bytes=2000 * 1024**2, supports_local_path_upload=False,
        transport_name="Local Bot API", configured_local_max_bytes=2000 * 1024**2,
    )
    too_large = build_delivery_limit_text(local, 320 * 1024**2)
    assert "temporarily unavailable" in too_large and "cloud" in too_large
    admin = build_admin_status_text(
        {"cpu": {"load_percent": 1}, "memory": {"available": 1024**3}},
        {"active": 0, "waiting": 0, "failed": 0, "successful": 1, "cache_hits": 0},
        {"extraction": 1, "download": 1, "merge": 1, "upload": 1},
        "manual", False, 1024**3, local, ("2026.8.19", 0),
    )
    assert "Local Bot API ⚠️ Unavailable" in admin
    assert "Generic extractor: available" in admin

    local.endpoint_available = True
    large_format = MediaFormat(
        format_id="large", vcodec_raw="h264", acodec_raw="aac",
        is_video=True, is_audio=True, is_muxed=True,
        filesize=320 * 1024**2, protocol="https",
    )
    details = build_format_details_text(
        large_format, None, telegram_capabilities=local,
    )
    assert "Protocol: https" in details
    assert "Large-file delivery available" in details
