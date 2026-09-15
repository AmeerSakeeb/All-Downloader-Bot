"""Single source of truth for active Telegram delivery capabilities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.config import Settings


EndpointProbe = Callable[[str], Awaitable[bool]]
STANDARD_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


@dataclass
class TelegramCapabilities:
    mode: str
    local_api_enabled: bool
    endpoint_available: bool
    max_upload_bytes: int
    supports_local_path_upload: bool
    transport_name: str
    configured_local_max_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "TelegramCapabilities":
        local = settings.use_local_api
        configured = settings.local_api_max_file_size_mb * 1024 * 1024
        return cls(
            mode="local" if local else "standard",
            local_api_enabled=local,
            endpoint_available=not local,
            # A configured Local ceiling is not active until readiness is proven.
            max_upload_bytes=0 if local else STANDARD_UPLOAD_LIMIT_BYTES,
            # FSInputFile streams from a contained job path. Server-local path
            # references require an explicitly shared, identical filesystem and
            # are therefore not claimed by the current deployment contract.
            supports_local_path_upload=False,
            transport_name="Local Bot API" if local else "Standard Bot API",
            configured_local_max_bytes=configured,
        )

    async def refresh(self, base_url: str, probe: EndpointProbe | None = None) -> bool:
        if not self.local_api_enabled:
            self.endpoint_available = True
            self.max_upload_bytes = STANDARD_UPLOAD_LIMIT_BYTES
            return True
        # Configuration or a generic HTTP response is not evidence that this
        # endpoint is the Telegram Bot API. The caller must provide a probe that
        # performs an authenticated, lightweight Telegram method through the
        # configured transport (TelegramService uses Bot.get_me()).
        try:
            self.endpoint_available = bool(probe and await probe(base_url))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.endpoint_available = False
        self.max_upload_bytes = (
            self.configured_local_max_bytes if self.endpoint_available else 0
        )
        return self.endpoint_available

    def can_upload(self, size_bytes: int | None) -> tuple[bool, str]:
        if self.local_api_enabled and not self.endpoint_available:
            return False, "Large-file Telegram delivery is temporarily unavailable."
        if size_bytes is None:
            return True, "Size unknown; delivery will be checked after download"
        if size_bytes > self.max_upload_bytes:
            size_mb = size_bytes / 1024**2
            limit_mb = self.max_upload_bytes / 1024**2
            return False, (
                f"Estimated size ({size_mb:.1f} MB) exceeds "
                f"{self.transport_name} limit ({limit_mb:.0f} MB)."
            )
        return True, "OK"

    def status_lines(self) -> tuple[str, str]:
        if self.local_api_enabled and not self.endpoint_available:
            return "Local Bot API ⚠️ Unavailable", "Upload limit: unavailable"
        return self.transport_name, f"Upload limit: {self.max_upload_bytes // 1024**2} MB"
