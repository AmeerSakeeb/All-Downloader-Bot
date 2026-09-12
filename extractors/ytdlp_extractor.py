"""Supervised yt-dlp extraction through the validating outbound proxy."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from core.config import Settings, get_settings
from core.exceptions import (
    AuthenticationRequiredError, DRMProtectedError, ExtractionError,
    ExtractionTimeoutError, SiteAccessChallengeError, UnsupportedUrlError,
)
from core.models import MediaAsset, MediaItem, MediaSession
from downloads.process_supervisor import ProcessSupervisor
from extractors.format_manager import normalize_format_inventory
from extractors.interface import Extractor
from security.proxy import ControlledOutboundProxy
from security.ssrf import SSRFGuard
from security.url_logging import sanitize_url_for_log
from services.ytdlp_policy import CookieFileUnavailableError, YtDlpPolicy
from services.cookie_profiles import CookieProfiles, CookieProfileError

logger = logging.getLogger(__name__)


class YtDlpExtractor(Extractor):
    def __init__(
        self,
        timeout_secs: int | None = None,
        *,
        settings: Settings | None = None,
        supervisor: ProcessSupervisor | None = None,
        proxy_url: str | None = None,
        profiles: CookieProfiles | None = None,
    ):
        self.settings = settings
        self.timeout_secs = timeout_secs
        self.supervisor = supervisor or ProcessSupervisor()
        self.proxy_url = proxy_url
        self.profiles = profiles

    async def can_extract(self, url: str) -> bool:
        return True

    async def extract(
        self, url: str, user_id: int, *, operation_id: str | None = None,
        prefer_impersonation: bool = False, cookie_profile: str | None = None,
    ) -> MediaSession:
        settings = self.settings or get_settings()
        timeout_secs = self.timeout_secs or settings.ytdlp_timeout
        SSRFGuard.validate_url(url)
        temporary_proxy: ControlledOutboundProxy | None = None
        proxy_url = self.proxy_url
        if not proxy_url:
            temporary_proxy = ControlledOutboundProxy()
            proxy_url = await temporary_proxy.start()
        try:
            process_owner = operation_id or f"extraction-{uuid.uuid4().hex}"
            policy = YtDlpPolicy(settings, proxy_url, self.profiles)
            impersonated = prefer_impersonation
            profile_name = cookie_profile
            for attempt_index in range(3):
                attempt_type = self._attempt_type(bool(profile_name), impersonated)
                try:
                    command = self._build_command(
                        url, settings, policy, impersonated=impersonated,
                        profile_name=profile_name,
                    )
                except (CookieFileUnavailableError, CookieProfileError) as error:
                    logger.error(
                        "yt-dlp extraction configuration failure site=%s attempt=%s category=cookie_file_unavailable",
                        urlsplit(url).hostname or "unknown", attempt_type,
                    )
                    raise ExtractionError(
                        "The configured cookie file is unavailable",
                        user_message="The operator-managed authorized session is unavailable.",
                    ) from error
                try:
                    result = await self.supervisor.run(
                        command,
                        job_id=process_owner,
                        stage="extraction",
                        timeout=timeout_secs,
                        max_output_bytes=8 * 1024 * 1024,
                    )
                except asyncio.TimeoutError as error:
                    self._log_attempt(
                        url, attempt_type, returncode=None, timed_out=True,
                        diagnostic="", category="extraction_timeout",
                    )
                    if settings.ytdlp_impersonation_fallback and not impersonated:
                        impersonated = True
                        continue
                    raise ExtractionTimeoutError() from error
                if result.returncode == 0:
                    try:
                        metadata = json.loads(result.stdout.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        self._log_attempt(
                            url, attempt_type, returncode=result.returncode,
                            timed_out=False, diagnostic="malformed metadata",
                            category="extraction_failed",
                        )
                        raise ExtractionError("yt-dlp returned malformed metadata") from error
                    self._log_attempt(
                        url, attempt_type, returncode=0, timed_out=False,
                        diagnostic="", category="success",
                    )
                    return self._build_session(
                        metadata, url, user_id, settings,
                        impersonated=impersonated, profile_name=profile_name,
                    )
                diagnostic = result.stderr.decode("utf-8", errors="replace")[-4000:]
                category = self._classify_failure(diagnostic)
                self._log_attempt(
                    url, attempt_type, returncode=result.returncode,
                    timed_out=False, diagnostic="<session diagnostic redacted>" if profile_name else self._safe_diagnostic(
                        diagnostic, settings.ytdlp_cookies_file
                    ), category=category,
                )
                if category == "authentication_required":
                    profile = self.profiles.select(url) if self.profiles and not profile_name else None
                    if profile:
                        profile_name = profile.name
                        continue
                    raise AuthenticationRequiredError()
                if category == "unsupported_url":
                    raise UnsupportedUrlError(sanitize_url_for_log(url))
                if category == "drm_unsupported":
                    raise DRMProtectedError(sanitize_url_for_log(url))
                if category == "site_access_challenge":
                    if settings.ytdlp_impersonation_fallback and not impersonated:
                        impersonated = True
                        continue
                    raise SiteAccessChallengeError()
                raise ExtractionError(
                    "yt-dlp could not analyze this media URL",
                    user_message="The website could not be analyzed safely. Please try again later.",
                )
            raise ExtractionError("The bounded extraction attempt budget was exhausted")
        except FileNotFoundError as error:
            raise ExtractionError("yt-dlp is not installed") from error
        finally:
            if temporary_proxy:
                await temporary_proxy.close()

    @staticmethod
    def _build_command(
        url: str, settings: Settings, policy: YtDlpPolicy, *, impersonated: bool,
        profile_name: str | None = None,
    ) -> list[str]:
        return [
            *policy.executable(),
            "--dump-single-json",
            "--skip-download",
            "--no-check-formats",
            "--playlist-end",
            str(settings.max_collection_items),
            "--no-warnings",
            *policy.common_args(impersonate=impersonated, url=url, profile_name=profile_name),
            url,
        ]

    def _build_session(
        self, metadata: dict[str, Any], url: str, user_id: int,
        settings: Settings, *, impersonated: bool, profile_name: str | None = None,
    ) -> MediaSession:
        self._validate_extracted_urls(metadata)
        raw_formats = metadata.get("formats") or ([metadata] if metadata.get("url") else [])
        formats = normalize_format_inventory(raw_formats)
        items = self._media_items(metadata, parent_url=url)
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
            ytdlp_impersonated=impersonated,
            cookie_profile=profile_name,
        )

    @staticmethod
    def _attempt_type(authenticated: bool, impersonated: bool) -> str:
        if authenticated and impersonated:
            return "authenticated-impersonated"
        if authenticated:
            return "authenticated"
        return "impersonated" if impersonated else "normal"

    @staticmethod
    def _classify_failure(diagnostic: str) -> str:
        lowered = diagnostic.lower()
        if any(value in lowered for value in (
            "sign in to confirm", "login required", "log in to", "cookies-from-browser",
            "use --cookies", "authentication required",
        )):
            return "authentication_required"
        if "unsupported url" in lowered or "no suitable extractor" in lowered:
            return "unsupported_url"
        if any(value in lowered for value in (
            "drm protected", "drm-protected", "digital rights management",
            "this video is drm",
        )):
            return "drm_unsupported"
        if any(value in lowered for value in (
            "http error 403", "http error 429", "forbidden", "too many requests",
            "impersonat", "tls", "ssl", "handshake", "certificate verify",
            "access denied", "request blocked", "anti-bot", "captcha", "not a bot",
        )):
            return "site_access_challenge"
        return "extraction_failed"

    @staticmethod
    def _safe_diagnostic(value: str, cookie_file) -> str:
        lines: list[str] = []
        cookie_text = str(cookie_file) if cookie_file else ""
        for line in value.splitlines()[-5:]:
            lowered = line.lower()
            if "authorization:" in lowered or "cookie:" in lowered:
                lines.append("<sensitive diagnostic redacted>")
                continue
            if cookie_text:
                line = line.replace(cookie_text, "<cookie-file>")
            line = re.sub(
                r"https?://[^\s]+",
                lambda match: sanitize_url_for_log(match.group(0).rstrip(".,;)]")),
                line,
            )
            lines.append(line[:500])
        return " | ".join(lines)

    @staticmethod
    def _log_attempt(
        url: str, attempt_type: str, *, returncode: int | None,
        timed_out: bool, diagnostic: str, category: str,
    ) -> None:
        logger.info(
            "yt-dlp extraction attempt site=%s attempt=%s returncode=%s timeout=%s category=%s diagnostic=%s",
            urlsplit(url).hostname or "unknown", attempt_type,
            returncode if returncode is not None else "none", timed_out,
            category, diagnostic or "none",
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
    def _media_items(metadata: dict[str, Any], *, parent_url: str) -> list[MediaItem]:
        items: list[MediaItem] = []
        for position, entry in enumerate(metadata.get("entries") or [], 1):
            if not isinstance(entry, dict):
                continue
            raw_formats = entry.get("formats") or []
            formats = normalize_format_inventory(raw_formats)
            webpage_url = entry.get("webpage_url")
            direct_source_url = entry.get("url") or entry.get("original_url")
            ext = str(entry.get("ext") or "").lower()
            kind = "image" if ext in {"jpg", "jpeg", "png", "webp", "gif"} else "video"
            source = webpage_url or (direct_source_url if kind == "image" else parent_url)
            if not isinstance(source, str) or not source.startswith(("http://", "https://")):
                continue
            heights = [fmt.height for fmt in formats if fmt.is_video and fmt.height]
            entry_id = str(entry.get("id")) if entry.get("id") is not None else None
            entry_index = entry.get("playlist_index")
            if not isinstance(entry_index, int) or entry_index < 1:
                entry_index = position
            items.append(MediaItem(
                kind=kind,
                source_url=source,
                webpage_url=webpage_url if isinstance(webpage_url, str) else None,
                direct_source_url=direct_source_url if isinstance(direct_source_url, str) else None,
                parent_collection_url=parent_url,
                collection_entry_index=entry_index,
                collection_entry_id=entry_id,
                title=entry.get("title") or f"Item {len(items) + 1}",
                extractor_id=entry_id,
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
