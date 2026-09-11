"""Shared, security-preserving yt-dlp transport command policy."""

from __future__ import annotations

from pathlib import Path

from core.config import Settings


class CookieFileUnavailableError(ValueError):
    """Configured operator cookie file is missing or not a regular file."""


class YtDlpPolicy:
    """Build common yt-dlp arguments without affecting media identity."""

    def __init__(self, settings: Settings, proxy_url: str | None):
        self.settings = settings
        self.proxy_url = proxy_url

    @property
    def authenticated(self) -> bool:
        return self.settings.ytdlp_cookies_file is not None

    def common_args(self, *, impersonate: bool = False) -> list[str]:
        args = [
            "--ignore-config",
            "--socket-timeout",
            str(self.settings.ytdlp_socket_timeout),
            "--extractor-retries",
            str(self.settings.ytdlp_extractor_retries),
            "--js-runtimes",
            "deno",
        ]
        if self.proxy_url:
            args.extend(("--proxy", self.proxy_url))
        cookie_file = self.settings.ytdlp_cookies_file
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
        return args
