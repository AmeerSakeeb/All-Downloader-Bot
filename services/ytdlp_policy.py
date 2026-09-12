"""Shared, security-preserving yt-dlp transport command policy."""

from __future__ import annotations

from pathlib import Path
import sys

from core.config import Settings
from services.cookie_profiles import CookieProfiles, CookieProfileError


class CookieFileUnavailableError(ValueError):
    """Configured operator cookie file is missing or not a regular file."""


class YtDlpPolicy:
    """Build common yt-dlp arguments without affecting media identity."""

    def __init__(self, settings: Settings, proxy_url: str | None, profiles: CookieProfiles | None = None):
        self.settings = settings
        self.proxy_url = proxy_url
        self.profiles = profiles

    @staticmethod
    def executable() -> list[str]:
        return [sys.executable, "-m", "services.ytdlp_runner"]

    @property
    def authenticated(self) -> bool:
        return self.settings.ytdlp_cookies_file is not None

    def common_args(self, *, impersonate: bool = False, url: str = "",
                    profile_name: str | None = None) -> list[str]:
        args = [
            "--ignore-config",
            "--socket-timeout",
            str(self.settings.ytdlp_socket_timeout),
            "--extractor-retries",
            str(self.settings.ytdlp_extractor_retries),
            "--js-runtimes",
            "deno",
            "--retries", str(self.settings.max_retries),
            "--fragment-retries", str(self.settings.max_retries),
        ]
        if self.proxy_url:
            args.extend(("--proxy", self.proxy_url))
        cookie_file = None
        if profile_name:
            if not self.profiles:
                raise CookieProfileError("Authorized session configuration is unavailable")
            profile = self.profiles.select(url, profile_name)
            profile.validate_file()
            cookie_file = profile.path
        if cookie_file is None:
            args.append("--no-cookies")
        else:
            path = Path(cookie_file)
            if not path.is_file():
                raise CookieFileUnavailableError(
                    "The configured yt-dlp cookie file is unavailable"
                )
            args.extend(("--cookies", str(path)))
        if impersonate:
            args.extend(("--impersonate", self.settings.ytdlp_impersonate_target))
        if self.settings.bandwidth_ceiling > 0:
            args.extend(("--limit-rate", str(self.settings.bandwidth_ceiling)))
        return args
