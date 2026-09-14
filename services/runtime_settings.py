"""Validated, persistent live operational settings.

Environment values are bootstrap defaults. Once an administrator stores an
override here, the SQLite value is authoritative until it is reset.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from core.config import ResourceMode, Settings
from storage.database import Database


SettingKind = Literal["int", "enum"]
ChangeListener = Callable[[str, object], Awaitable[None] | None]


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    kind: SettingKind
    default_attribute: str
    minimum: int | None
    maximum: int | None
    scope: str
    description: str
    default: object = None
    restart_required: bool = False
    choices: tuple[str, ...] = ()


RUNTIME_SETTING_REGISTRY: dict[str, RuntimeSettingSpec] = {
    spec.key: spec for spec in (
        RuntimeSettingSpec(
            "max_batch_urls", "int", "max_batch_urls", 1, 10, "global",
            "Maximum URLs accepted in one Telegram message.", default=4,
        ),
        RuntimeSettingSpec(
            "max_concurrent_extractions", "int", "max_concurrent_extractions", 1, 4,
            "global", "Target extraction concurrency.",
            default=2,
        ),
        RuntimeSettingSpec(
            "max_concurrent_downloads", "int", "max_concurrent_downloads", 1, 4,
            "global", "Target download concurrency.",
            default=2,
        ),
        RuntimeSettingSpec(
            "max_concurrent_merges", "int", "max_concurrent_merges", 1, 2,
            "global", "Target lossless merge concurrency.",
            default=1,
        ),
        RuntimeSettingSpec(
            "max_concurrent_uploads", "int", "max_concurrent_uploads", 1, 3,
            "global", "Target Telegram upload concurrency.",
            default=1,
        ),
        RuntimeSettingSpec(
            "max_queued_jobs_per_user", "int", "max_queued_jobs_per_user", 1, 50,
            "global", "Maximum active/waiting requests per user.",
            default=6,
        ),
        RuntimeSettingSpec(
            "max_total_queued_jobs", "int", "max_total_queued_jobs", 1, 1000,
            "global", "Maximum active/waiting jobs for the bot.",
            default=50,
        ),
        RuntimeSettingSpec(
            "bandwidth_ceiling", "int", "bandwidth_ceiling", 0, 1_000_000_000,
            "global", "Per-downloader byte-per-second ceiling; zero is unlimited.",
            default=0,
        ),
        RuntimeSettingSpec(
            "resource_mode", "enum", "resource_mode", None, None, "global",
            "Adaptive resource policy.", choices=tuple(mode.value for mode in ResourceMode),
            default=ResourceMode.AUTO_SHARED,
        ),
    )
}


class RuntimeSettingError(ValueError):
    """Safe validation failure for an operational setting."""


class RuntimeSettingsService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._bootstrap = {
            key: getattr(settings, spec.default_attribute)
            for key, spec in RUNTIME_SETTING_REGISTRY.items()
        }
        self._overrides: set[str] = set()
        self._listeners: list[ChangeListener] = []

    async def initialize(self) -> None:
        stored = await self.db.list_runtime_settings()
        for key, raw in stored.items():
            if key not in RUNTIME_SETTING_REGISTRY:
                continue
            value = self.validate(key, raw)
            self._apply(key, value)
            self._overrides.add(key)

    def add_listener(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def spec(self, key: str) -> RuntimeSettingSpec:
        try:
            return RUNTIME_SETTING_REGISTRY[key]
        except KeyError as error:
            raise RuntimeSettingError("Unknown runtime setting") from error

    def validate(self, key: str, raw: object) -> object:
        spec = self.spec(key)
        if spec.kind == "int":
            if isinstance(raw, bool):
                raise RuntimeSettingError("A whole number is required")
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError) as error:
                raise RuntimeSettingError("A whole number is required") from error
            if spec.minimum is not None and value < spec.minimum:
                raise RuntimeSettingError(f"Minimum value is {spec.minimum}")
            if spec.maximum is not None and value > spec.maximum:
                raise RuntimeSettingError(f"Maximum value is {spec.maximum}")
            return value
        value = str(raw).strip().lower()
        if value not in spec.choices:
            raise RuntimeSettingError("Unsupported setting value")
        return ResourceMode(value) if key == "resource_mode" else value

    def get(self, key: str) -> object:
        spec = self.spec(key)
        return getattr(self.settings, spec.default_attribute)

    def bootstrap_value(self, key: str) -> object:
        self.spec(key)
        return self._bootstrap[key]

    def is_overridden(self, key: str) -> bool:
        return key in self._overrides

    async def set(self, key: str, raw: object, *, updated_by: int) -> object:
        value = self.validate(key, raw)
        serialized = value.value if isinstance(value, ResourceMode) else str(value)
        await self.db.set_runtime_setting(key, serialized, updated_by)
        self._apply(key, value)
        self._overrides.add(key)
        await self._notify(key, value)
        return value

    async def reset(self, key: str, *, updated_by: int) -> object:
        del updated_by  # Reset removes the override; the audit timestamp disappears with it.
        self.spec(key)
        await self.db.delete_runtime_setting(key)
        value = self._bootstrap[key]
        self._apply(key, value)
        self._overrides.discard(key)
        await self._notify(key, value)
        return value

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {
            key: {
                "value": self.get(key),
                "default": self.bootstrap_value(key),
                "overridden": self.is_overridden(key),
                "spec": spec,
            }
            for key, spec in RUNTIME_SETTING_REGISTRY.items()
        }

    def _apply(self, key: str, value: object) -> None:
        setattr(self.settings, self.spec(key).default_attribute, value)

    async def _notify(self, key: str, value: object) -> None:
        for listener in tuple(self._listeners):
            result = listener(key, value)
            if inspect.isawaitable(result):
                await result
