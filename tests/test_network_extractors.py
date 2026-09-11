import ipaddress
import json

import pytest

from core.models import AudioCodec, VideoCodec
from downloads.process_supervisor import ProcessResult
from extractors.direct_extractor import DirectMediaExtractor
from extractors.ytdlp_extractor import YtDlpExtractor
from security.proxy import ControlledOutboundProxy


@pytest.mark.asyncio
async def test_direct_media_does_not_fabricate_codecs(monkeypatch, settings):
    extractor = DirectMediaExtractor(settings, "http://proxy")

    async def fake_head(url, proxy):
        return url, {"Content-Type": "video/mp4", "Content-Length": "123"}

    monkeypatch.setattr(extractor, "_head_with_safe_redirects", fake_head)
    monkeypatch.setattr("extractors.direct_extractor.SSRFGuard.validate_url", lambda url: [])
    session = await extractor.extract("https://example.com/video.mp4", 1)
    item = session.formats[0]
    assert item.vcodec_raw is None and item.vcodec_normalized == VideoCodec.OTHER
    assert item.acodec_raw is None and item.acodec_normalized == AudioCodec.NONE


@pytest.mark.asyncio
async def test_manifest_is_delegated_to_ytdlp(settings):
    extractor = DirectMediaExtractor(settings)
    assert not await extractor.can_extract("https://example.com/master.m3u8")
    assert not await extractor.can_extract("https://example.com/manifest.mpd")


@pytest.mark.asyncio
async def test_ytdlp_extractions_use_independent_operation_owners(monkeypatch, settings):
    class Supervisor:
        def __init__(self):
            self.owners = []

        async def run(self, command, **kwargs):
            self.owners.append(kwargs["job_id"])
            payload = {
                "formats": [
                    {"format_id": "v", "vcodec": "h264", "acodec": "aac", "ext": "mp4"}
                ]
            }
            return ProcessResult(0, json.dumps(payload).encode(), b"")

    supervisor = Supervisor()
    monkeypatch.setattr("extractors.ytdlp_extractor.SSRFGuard.validate_url", lambda url: [])
    extractor = YtDlpExtractor(settings=settings, supervisor=supervisor, proxy_url="http://proxy")
    await extractor.extract("https://example.com/a", 1, operation_id="analysis-a")
    await extractor.extract("https://example.com/b", 1, operation_id="analysis-b")
    assert supervisor.owners == ["analysis-a", "analysis-b"]


@pytest.mark.asyncio
async def test_proxy_blocks_unsafe_connect_before_outbound():
    proxy = ControlledOutboundProxy()
    url = await proxy.start()
    port = int(url.rsplit(":", 1)[1])
    reader, writer = await __import__("asyncio").open_connection("127.0.0.1", port)
    writer.write(b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    response = await reader.read(1024)
    assert b"403 Forbidden" in response
    writer.close()
    await writer.wait_closed()
    await proxy.close()


@pytest.mark.asyncio
async def test_proxy_pins_validated_ip(monkeypatch):
    calls = []

    async def fake_open(host, port):
        calls.append((host, port))
        return object(), object()

    monkeypatch.setattr("security.proxy.asyncio.open_connection", fake_open)
    ip = ipaddress.ip_address("93.184.216.34")
    await ControlledOutboundProxy._connect_pinned([ip], 443)
    assert calls == [("93.184.216.34", 443)]


def test_all_resolved_addresses_must_be_public(monkeypatch):
    monkeypatch.setattr(
        "security.ssrf.resolve_hostname",
        lambda host: [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("127.0.0.1")],
    )
    with pytest.raises(Exception):
        from security.ssrf import SSRFGuard

        SSRFGuard.validate_url("https://example.com")
