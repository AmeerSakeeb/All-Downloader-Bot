"""Supervised yt-dlp extraction through the validating outbound proxy."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from core.config import Settings, get_settings
from core.exceptions import (
    AuthenticationRequiredError, DRMProtectedError, ExtractionError, UnsupportedUrlError,
)
from core.models import MediaAsset, MediaItem, MediaSession
from downloads.process_supervisor import ProcessSupervisor
from extractors.format_manager import normalize_format_inventory
from extractors.interface import Extractor
from security.proxy import ControlledOutboundProxy
from security.ssrf import SSRFGuard


class YtDlpExtractor(Extractor):
    def __init__(
        self,
        timeout_secs: int | None = None,
        *,
        settings: Settings | None = None,
        supervisor: ProcessSupervisor | None = None,
        proxy_url: str | None = None,
    ):
        self.settings = settings
        self.timeout_secs = timeout_secs
        self.supervisor = supervisor or ProcessSupervisor()
        self.proxy_url = proxy_url

    async def can_extract(self, url: str) -> bool:
        return True

    async def extract(
        self, url: str, user_id: int, *, operation_id: str | None = None
    ) -> MediaSession:
        settings = self.settings or get_settings()
        timeout_secs = self.timeout_secs or settings.ytdlp_timeout
        SSRFGuard.validate_url(url)
        temporary_proxy: ControlledOutboundProxy | None = None
        proxy_url = self.proxy_url
        if not proxy_url:
            temporary_proxy = ControlledOutboundProxy()
            proxy_url = await temporary_proxy.start()
        command = [
            "yt-dlp",
            "--dump-single-json",
            "--playlist-end",
            str(settings.max_collection_items),
            "--no-warnings",
            "--ignore-config",
            "--no-cookies",
            "--proxy",
            proxy_url,
            url,
        ]
        try:
            process_owner = operation_id or f"extraction-{uuid.uuid4().hex}"
            result = await self.supervisor.run(
                command,
                job_id=process_owner,
                stage="extraction",
                timeout=timeout_secs,
                max_output_bytes=8 * 1024 * 1024,
            )
        except FileNotFoundError as error:
            raise ExtractionError("yt-dlp is not installed") from error
        except asyncio.TimeoutError as error:
            raise ExtractionError("Media analysis timed out") from error
        finally:
            if temporary_proxy:
                await temporary_proxy.close()
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", errors="replace")[-2000:]
            lowered = diagnostic.lower()
            if "unsupported url" in lowered:
                raise UnsupportedUrlError(url)
            if "drm" in lowered or "decrypt" in lowered:
                raise DRMProtectedError(url)
            if any(value in lowered for value in ("sign in", "log in", "login required", "cookies")):
                raise AuthenticationRequiredError()
            raise ExtractionError("yt-dlp could not analyze this public media URL")
        try:
            metadata = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExtractionError("yt-dlp returned malformed metadata") from error
        self._validate_extracted_urls(metadata)
        raw_formats = metadata.get("formats") or ([metadata] if metadata.get("url") else [])
        formats = normalize_format_inventory(raw_formats)
        items = self._media_items(metadata)
        if not formats and not items:
            raise ExtractionError("No downloadable formats were found")
        thumbnails = self._thumbnail_assets(metadata.get("thumbnails") or [])
        subtitles = self._subtitle_assets(metadata)
        playlist_count = int(metadata.get("playlist_count") or metadata.get("n_entries") or 0)
        session_kind = "single"
        if items:
            extractor_name = str(metadata.get("extractor") or "").lower()
            has_images = any(item.kind == "image" for item in items)
            session_kind = "multimedia" if has_images or "instagram" in extractor_name else "playlist"
        return MediaSession.with_ttl(
            ttl_seconds=settings.media_session_ttl,
            user_id=user_id,
            url=url,
            canonical_url=metadata.get("webpage_url") or url,
            extractor=metadata.get("extractor") or "yt-dlp",
            title=metadata.get("title") or "Unknown title",
            duration=metadata.get("duration"),
            uploader=metadata.get("uploader") or metadata.get("creator"),
            thumbnail_url=metadata.get("thumbnail"),
            description=metadata.get("description"),
            upload_date=metadata.get("upload_date"),
            media_id=str(metadata.get("id")) if metadata.get("id") is not None else None,
            session_kind=session_kind,
            collection_truncated=playlist_count > len(items) if playlist_count else False,
            thumbnails=thumbnails,
            subtitles=subtitles,
            items=items,
            formats=formats,
        )

    @staticmethod
    def _thumbnail_assets(values: list[dict[str, Any]]) -> list[MediaAsset]:
        assets: list[MediaAsset] = []
        for item in values:
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            ext = str(item.get("ext") or url.split("?", 1)[0].rsplit(".", 1)[-1] or "jpg")
            assets.append(MediaAsset(
                kind="thumbnail", source_url=url, ext=ext,
                label=item.get("id") or item.get("resolution"),
                width=item.get("width"), height=item.get("height"),
            ))
        return assets

    @staticmethod
    def _subtitle_assets(metadata: dict[str, Any]) -> list[MediaAsset]:
        assets: list[MediaAsset] = []
        for key, autogenerated in (("subtitles", False), ("automatic_captions", True)):
            for language, tracks in (metadata.get(key) or {}).items():
                for track in tracks or []:
                    url = track.get("url")
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        assets.append(MediaAsset(
                            kind="subtitle", source_url=url, ext=str(track.get("ext") or "vtt"),
                            language=str(language), autogenerated=autogenerated,
                            label="Auto-generated" if autogenerated else "Creator subtitles",
                        ))
        return assets

    @staticmethod
    def _media_items(metadata: dict[str, Any]) -> list[MediaItem]:
        items: list[MediaItem] = []
        for entry in metadata.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            raw_formats = entry.get("formats") or []
            formats = normalize_format_inventory(raw_formats)
            source = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
            if not isinstance(source, str) or not source.startswith(("http://", "https://")):
                continue
            ext = str(entry.get("ext") or "").lower()
            kind = "image" if ext in {"jpg", "jpeg", "png", "webp", "gif"} else "video"
            heights = [fmt.height for fmt in formats if fmt.is_video and fmt.height]
            items.append(MediaItem(
                kind=kind,
                source_url=source,
                title=entry.get("title") or f"Item {len(items) + 1}",
                extractor_id=str(entry.get("id")) if entry.get("id") is not None else None,
                max_height=max(heights) if heights else None,
                formats=formats,
                thumbnail_url=entry.get("thumbnail"),
            ))
        return items

    @staticmethod
    def _validate_extracted_urls(metadata: dict[str, Any]) -> None:
        candidates: list[str] = []
        for key in ("url", "manifest_url", "fragment_base_url", "webpage_url", "original_url", "thumbnail"):
            if isinstance(metadata.get(key), str):
                candidates.append(metadata[key])
        for item in metadata.get("formats") or []:
            for key in ("url", "manifest_url", "fragment_base_url", "webpage_url", "original_url"):
                if isinstance(item.get(key), str):
                    candidates.append(item[key])
        for item in metadata.get("thumbnails") or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                candidates.append(item["url"])
        for key in ("subtitles", "automatic_captions"):
            for tracks in (metadata.get(key) or {}).values():
                for track in tracks or []:
                    if isinstance(track, dict) and isinstance(track.get("url"), str):
                        candidates.append(track["url"])
        for entry in metadata.get("entries") or []:
            if isinstance(entry, dict):
                YtDlpExtractor._validate_extracted_urls(entry)
        for candidate in candidates:
            if candidate.startswith(("http://", "https://")):
                SSRFGuard.validate_url(candidate)
