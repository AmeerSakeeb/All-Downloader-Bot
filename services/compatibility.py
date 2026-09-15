"""Version-gated, single-winner metadata compatibility overrides."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable


MetadataRepair = Callable[[dict[str, Any]], dict[str, Any]]


def installed_ytdlp_version() -> str:
    try:
        return version("yt-dlp")
    except PackageNotFoundError:
        return "unavailable"


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.replace("-", ".").split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


@dataclass(frozen=True)
class CompatibilityOverride:
    """Minimal documented repair enabled only for an explicit version window."""

    override_id: str
    site_policy_id: str
    reason: str
    tested_ytdlp_version: str
    upstream_reference: str
    enabled_from: str | None
    enabled_before: str | None
    repair_metadata: MetadataRepair

    def supports(self, ytdlp_version: str) -> bool:
        current = _version_key(ytdlp_version)
        if not current:
            return False
        if self.enabled_from and current < _version_key(self.enabled_from):
            return False
        if self.enabled_before and current >= _version_key(self.enabled_before):
            return False
        return True


class CompatibilityOverrideRegistry:
    """Apply at most one policy-selected repair; production starts with none."""

    def __init__(
        self,
        overrides: tuple[CompatibilityOverride, ...] = (),
        *,
        ytdlp_version: str | None = None,
    ):
        self.ytdlp_version = ytdlp_version or installed_ytdlp_version()
        self._overrides: dict[str, CompatibilityOverride] = {}
        for override in overrides:
            if override.override_id in self._overrides:
                raise ValueError(f"Duplicate compatibility override: {override.override_id}")
            self._overrides[override.override_id] = override

    def active_for(self, override_id: str | None) -> CompatibilityOverride | None:
        if not override_id:
            return None
        override = self._overrides.get(override_id)
        return override if override and override.supports(self.ytdlp_version) else None

    def apply(
        self,
        override_id: str | None,
        metadata: dict[str, Any],
        *,
        site_policy_id: str | None = None,
    ) -> dict[str, Any]:
        override = self.active_for(override_id)
        if override and site_policy_id and override.site_policy_id != site_policy_id:
            raise ValueError("Compatibility override does not belong to the resolved site policy")
        return override.repair_metadata(metadata) if override else metadata

    @property
    def enabled_count(self) -> int:
        return sum(item.supports(self.ytdlp_version) for item in self._overrides.values())
