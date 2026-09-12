"""Operator-owned, domain-scoped runtime session references. No cookie persistence."""

from __future__ import annotations

import asyncio
import json
import re
import warnings
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlsplit

from core.config import Settings


class CookieProfileError(ValueError):
    """Safe configuration error, never includes cookie data or source text."""


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class CookieProfile:
    name: str
    domains: tuple[str, ...]
    path: Path
    enabled: bool = True

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return any(domain_matches(host, domain) for domain in self.domains)

    def validate_file(self) -> None:
        # Inspect locally only; errors must never quote a Netscape cookie line.
        try:
            if not self.path.is_file() or self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError
            jar = MozillaCookieJar(str(self.path))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                jar.load(ignore_discard=True, ignore_expires=True)
            if not jar or any(
                not any(domain_matches(cookie.domain.lstrip(".").lower(), d) for d in self.domains)
                for cookie in jar
            ):
                raise ValueError
        except Exception:
            raise CookieProfileError("Session file is unavailable, invalid, or outside its domain scope") from None


class CookieProfiles:
    def __init__(self, settings: Settings, db=None):
        self.settings = settings
        self.db = db
        self.profiles: dict[str, CookieProfile] = {}
        self._disabled: set[str] = set()

    def _read_configuration(self) -> dict[str, CookieProfile]:
        try:
            rows = []
            if self.settings.ytdlp_profiles_file:
                path = self.settings.ytdlp_profiles_file
                if path.stat().st_size > 65536:
                    raise ValueError
                rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or len(rows) > 20:
                raise ValueError
            if self.settings.ytdlp_cookies_file:
                rows.append({"name": "default", "domains": self.settings.ytdlp_cookie_domains,
                             "path": str(self.settings.ytdlp_cookies_file)})
            profiles = {}
            for row in rows:
                name = row["name"]
                domains = row["domains"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
                    raise ValueError
                if name in profiles or not isinstance(domains, list) or not domains or len(domains) > 12:
                    raise ValueError
                if any(not isinstance(d, str) or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", d
                ) for d in domains):
                    raise ValueError
                path = Path(row["path"])
                if not path.is_absolute() or len(str(path)) > 240 or not isinstance(row.get("enabled", True), bool):
                    raise ValueError
                if path.resolve().is_relative_to(self.settings.jobs_dir.resolve()):
                    raise ValueError
                profiles[name] = CookieProfile(name, tuple(domains), path, row.get("enabled", True))
            return profiles
        except Exception:
            raise CookieProfileError("Invalid session profile configuration; check names, domains and absolute paths") from None

    async def refresh(self) -> None:
        profiles = await asyncio.to_thread(self._read_configuration)
        disabled = set()
        if self.db:
            for name in profiles:
                if await self.db.get_system_setting("session_disabled:" + name) == "true":
                    disabled.add(name)
        self.profiles, self._disabled = profiles, disabled

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.profiles:
            raise CookieProfileError("Session profile no longer exists")
        if self.db:
            await self.db.set_system_setting("session_disabled:" + name, "false" if enabled else "true")
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def enabled(self, name: str) -> bool:
        return self.profiles[name].enabled and name not in self._disabled

    def select(self, url: str, name: str | None = None) -> CookieProfile | None:
        if name:
            profile = self.profiles.get(name)
            if not profile or not self.enabled(name) or not profile.matches(url):
                raise CookieProfileError("The selected authorized session is unavailable for this source")
            return profile
        return next((p for p in self.profiles.values() if self.enabled(p.name) and p.matches(url)), None)
