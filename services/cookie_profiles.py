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
    source_domains: tuple[str, ...]
    path: Path
    cookie_domains: tuple[str, ...] = ()
    enabled: bool = True

    def __init__(
        self,
        name: str,
        source_domains: tuple[str, ...] | list[str] = (),
        path: Path | str = Path(""),
        cookie_domains: tuple[str, ...] | list[str] | bool = (),
        enabled: bool = True,
        *,
        domains: tuple[str, ...] | list[str] | None = None,
    ):
        if domains is not None and not source_domains:
            source_domains = domains
        if isinstance(cookie_domains, bool):
            enabled = cookie_domains
            cookie_domains = ()
        src = tuple(source_domains) if isinstance(source_domains, (list, tuple)) else ()
        ck = tuple(cookie_domains) if isinstance(cookie_domains, (list, tuple)) and cookie_domains else src
        p = Path(path) if isinstance(path, str) else path
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_domains", src)
        object.__setattr__(self, "path", p)
        object.__setattr__(self, "cookie_domains", ck)
        object.__setattr__(self, "enabled", bool(enabled))

    @property
    def domains(self) -> tuple[str, ...]:
        return self.source_domains

    def matches(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return any(domain_matches(host, domain) for domain in self.source_domains)

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
                not any(domain_matches(cookie.domain.lstrip(".").lower(), d) for d in self.cookie_domains)
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
                cookie_path = self.settings.ytdlp_cookies_file.resolve()
                rows.append({
                    "name": "default",
                    "domains": list(self.settings.ytdlp_cookie_domains),
                    "path": str(cookie_path),
                })
            profiles = {}
            domain_regex = re.compile(
                r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
            )
            for row in rows:
                name = row.get("name")
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
                    raise ValueError
                if name in profiles:
                    raise ValueError

                source_raw = row.get("source_domains", row.get("domains"))
                cookie_raw = row.get("cookie_domains")
                if cookie_raw is None:
                    cookie_raw = source_raw
                if not isinstance(source_raw, list) or not source_raw or len(source_raw) > 12:
                    raise ValueError
                if not isinstance(cookie_raw, list) or not cookie_raw or len(cookie_raw) > 12:
                    raise ValueError

                for d in source_raw:
                    if not isinstance(d, str) or not domain_regex.fullmatch(d.strip().lower().rstrip(".")):
                        raise ValueError
                for d in cookie_raw:
                    if not isinstance(d, str) or not domain_regex.fullmatch(d.strip().lower().rstrip(".")):
                        raise ValueError

                source_domains = tuple(d.strip().lower().rstrip(".") for d in source_raw)
                cookie_domains = tuple(d.strip().lower().rstrip(".") for d in cookie_raw)

                path = Path(row["path"])
                if not path.is_absolute() or len(str(path)) > 240 or not isinstance(row.get("enabled", True), bool):
                    raise ValueError
                if path.resolve().is_relative_to(self.settings.jobs_dir.resolve()):
                    raise ValueError
                profiles[name] = CookieProfile(
                    name=name,
                    source_domains=source_domains,
                    path=path,
                    cookie_domains=cookie_domains,
                    enabled=row.get("enabled", True),
                )
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
