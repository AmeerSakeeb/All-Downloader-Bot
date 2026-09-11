"""Truthful fallback metadata extraction for opaque direct media files."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Mapping
from urllib.parse import urljoin, urlsplit

import aiohttp

from core.config import Settings, get_settings
from core.exceptions import ExtractionError
from core.models import AudioCodec, MediaFormat, MediaSession, VideoCodec
from extractors.interface import Extractor
from security.proxy import ControlledOutboundProxy
from security.ssrf import SSRFGuard


class DirectMediaExtractor(Extractor):
    def __init__(self, settings: Settings | None = None, proxy_url: str | None = None):
        self.settings = settings
        self.proxy_url = proxy_url

    async def can_extract(self, url: str) -> bool:
        suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
        # Adaptive manifests are delegated to yt-dlp so their genuine variants survive.
        return suffix in {".mp4", ".mkv", ".webm", ".mp3", ".aac", ".m4a", ".ogg"}

    async def extract(
        self, url: str, user_id: int, *, operation_id: str | None = None,
        prefer_impersonation: bool = False,
    ) -> MediaSession:
        settings = self.settings or get_settings()
        SSRFGuard.validate_url(url)
        temporary_proxy: ControlledOutboundProxy | None = None
        proxy_url = self.proxy_url
        if not proxy_url:
            temporary_proxy = ControlledOutboundProxy()
            proxy_url = await temporary_proxy.start()
        try:
            final_url, headers = await self._head_with_safe_redirects(url, proxy_url)
        finally:
            if temporary_proxy:
                await temporary_proxy.close()

        parsed = urlsplit(final_url)
        filename = PurePosixPath(parsed.path).name or "direct_media"
        ext = PurePosixPath(parsed.path).suffix.lower().lstrip(".") or "bin"
        content_type = headers.get("Content-Type", "").split(";", 1)[0].lower()
        is_audio = content_type.startswith("audio/") or ext in {"mp3", "aac", "m4a", "ogg"}
        is_video = content_type.startswith("video/") or not is_audio
        size_value = headers.get("Content-Length")
        filesize = int(size_value) if size_value and size_value.isdigit() else None
        media_format = MediaFormat(
            format_id="direct",
            is_video=is_video,
            is_audio=is_audio,
            is_muxed=is_video and is_audio,
            requires_separate_audio=False,
            vcodec_raw=None,
            vcodec_normalized=VideoCodec.OTHER if is_video else VideoCodec.NONE,
            acodec_raw=None,
            acodec_normalized=AudioCodec.OTHER if is_audio else AudioCodec.NONE,
            ext=ext,
            protocol=parsed.scheme,
            filesize=filesize,
            format_note="Direct media (codecs unverified)",
        )
        return MediaSession.with_ttl(
            ttl_seconds=settings.media_session_ttl,
            user_id=user_id,
            url=url,
            canonical_url=final_url,
            extractor="direct",
            title=filename,
            formats=[media_format],
        )

    async def _head_with_safe_redirects(
        self, initial_url: str, proxy_url: str
    ) -> tuple[str, Mapping[str, str]]:
        current = initial_url
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            settings = self.settings or get_settings()
            for _ in range(settings.max_redirects + 1):
                SSRFGuard.validate_url(current)
                async with client.head(
                    current, allow_redirects=False, proxy=proxy_url
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ExtractionError("Redirect response omitted Location")
                        next_url = urljoin(current, location)
                        # Validation occurs before aiohttp can contact the next hop.
                        SSRFGuard.validate_url(next_url)
                        current = next_url
                        continue
                    if response.status in {403, 405, 501}:
                        # Fallback to bounded Range GET probe when HEAD is disallowed
                        async with client.get(
                            current,
                            headers={"Range": "bytes=0-1023"},
                            allow_redirects=False,
                            proxy=proxy_url,
                        ) as get_resp:
                            if get_resp.status in {200, 206}:
                                hdrs = dict(get_resp.headers)
                                cr = hdrs.get("Content-Range", "")
                                if "/" in cr:
                                    total_str = cr.rsplit("/", 1)[-1].strip()
                                    if total_str.isdigit():
                                        hdrs["Content-Length"] = total_str
                                return current, hdrs
                            if get_resp.status in {301, 302, 303, 307, 308}:
                                loc = get_resp.headers.get("Location")
                                if loc:
                                    next_url = urljoin(current, loc)
                                    SSRFGuard.validate_url(next_url)
                                    current = next_url
                                    continue
                    if response.status >= 400:
                        raise ExtractionError(
                            f"Direct media server returned HTTP {response.status}"
                        )
                    return current, response.headers
        raise ExtractionError("Direct media URL exceeded the redirect limit")
