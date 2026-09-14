"""Adaptive, stage-aware resource admission with leak-proof leases."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.config import ResourceMode, Settings, get_settings
from resources.detector import ResourceDetector
from storage.database import Database
from storage.file_manager import FileManager


@dataclass
class StageLease:
    governor: "ResourceGovernor"
    stage: str
    released: bool = False

    async def __aenter__(self) -> "StageLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self.governor._release(self.stage)


class ResourceGovernor:
    STAGES = ("extraction", "download", "merge", "upload")

    def __init__(
        self,
        db: Database,
        file_mgr: FileManager,
        settings: Optional[Settings] = None,
        detector: type[ResourceDetector] = ResourceDetector,
    ):
        self.db = db
        self.file_mgr = file_mgr
        self.settings = settings or get_settings()
        self.detector = detector
        self._active_counts = dict.fromkeys(self.STAGES, 0)
        self._lock = asyncio.Lock()
        self._stable_limits: dict[str, int] = {}
        self._last_limit_change = 0.0
        self._last_sample: Optional[dict[str, object]] = None
        self._capacity_listener: Optional[Callable[[], None]] = None

    @property
    def active_counts(self) -> dict[str, int]:
        return dict(self._active_counts)

    def register_stage_start(self, stage: str) -> None:
        if stage in self._active_counts:
            self._active_counts[stage] += 1

    def register_stage_end(self, stage: str) -> None:
        if stage in self._active_counts:
            self._active_counts[stage] = max(0, self._active_counts[stage] - 1)

    def set_capacity_listener(self, listener: Callable[[], None]) -> None:
        self._capacity_listener = listener

    async def _release(self, stage: str) -> None:
        async with self._lock:
            self.register_stage_end(stage)
        if self._capacity_listener:
            self._capacity_listener()

    async def adaptive_limits(self) -> dict[str, int]:
        sample = await self.detector.sample()
        self._last_sample = sample
        memory = sample["memory"]
        cpu = sample["cpu"]
        process = sample["process"]
        assert isinstance(memory, dict) and isinstance(cpu, dict) and isinstance(process, dict)

        available = max(0, int(memory["available"]))
        total = max(1, int(memory["total"]))
        cores = max(0.25, float(cpu["cores"]))
        load_fraction = min(1.0, max(0.0, float(cpu["load_percent"]) / 100.0))
        process_fraction = min(
            0.8,
            (int(process["process_rss"]) + int(process["child_rss"])) / total,
        )
        if self.settings.resource_mode == ResourceMode.AUTO_SHARED:
            share = 0.45
            pressure_factor = max(0.15, 1.0 - load_fraction - process_fraction)
            usable_memory = available * max(0.1, 1.0 - self.settings.memory_safety_headroom)
        elif self.settings.resource_mode == ResourceMode.AUTO_DEDICATED:
            share = 0.85
            pressure_factor = max(0.2, 1.0 - (load_fraction * 0.7) - (process_fraction * 0.5))
            usable_memory = available * max(0.1, 1.0 - self.settings.memory_safety_headroom)
        else:
            share = 1.0
            pressure_factor = 1.0
            usable_memory = available

        effective_cpu = max(0.25, cores * share * pressure_factor)
        memory_units = max(0.25, usable_memory / max(1, total * 0.08))
        child_penalty = max(1.0, 1.0 + int(process["child_count"]) * 0.15)
        raw = {
            "extraction": max(1, int(min(effective_cpu * 1.5, memory_units) / child_penalty)),
            "download": max(1, int(min(effective_cpu * 2.0, memory_units * 1.5) / child_penalty)),
            "merge": max(1, int(min(effective_cpu * 0.7, memory_units * 0.5) / child_penalty)),
            "upload": max(1, int(min(effective_cpu * 2.5, memory_units * 2.0) / child_penalty)),
        }
        configured = {
            "extraction": self.settings.max_concurrent_extractions,
            "download": self.settings.max_concurrent_downloads,
            "merge": self.settings.max_concurrent_merges,
            "upload": self.settings.max_concurrent_uploads,
        }
        if self.settings.resource_mode == ResourceMode.MANUAL:
            raw = {stage: max(1, configured[stage] or raw[stage]) for stage in self.STAGES}
        else:
            raw = {
                stage: min(value, configured[stage]) if configured[stage] else value
                for stage, value in raw.items()
            }

        now = time.monotonic()
        if not self._stable_limits:
            self._stable_limits = raw
        elif now - self._last_limit_change >= 5.0:
            # One-slot changes provide hysteresis without ever cancelling active work.
            self._stable_limits = {
                stage: previous + max(-1, min(1, raw[stage] - previous))
                for stage, previous in self._stable_limits.items()
            }
            self._last_limit_change = now
        # Hysteresis may delay increases, but an administrator lowering a
        # target must immediately block additional admissions. Active leases
        # are deliberately not cancelled.
        effective = dict(self._stable_limits)
        for stage, ceiling in configured.items():
            if ceiling:
                effective[stage] = min(effective[stage], ceiling)
        return effective

    async def acquire_stage(self, stage: str) -> tuple[Optional[StageLease], str]:
        if stage not in self._active_counts:
            return None, f"Unknown stage: {stage}"
        if stage in {"download", "merge"} and self.file_mgr.get_free_disk_space() <= self.disk_safety_bytes():
            return None, "protected disk headroom is under pressure"
        limits = await self.adaptive_limits()
        pressure_ok, reason = self._pressure_check(self._last_sample)
        if not pressure_ok and self.settings.resource_mode != ResourceMode.MANUAL:
            return None, reason
        async with self._lock:
            active = self._active_counts[stage]
            if active >= limits[stage]:
                return None, f"{stage} concurrency is {active}/{limits[stage]}"
            self._active_counts[stage] += 1
            return StageLease(self, stage), "Ready"

    async def scheduler_capacity(self, running_jobs: int) -> int:
        """Return total task capacity without evicting tasks when pressure rises."""
        limits = await self.adaptive_limits()
        pressure_ok, _ = self._pressure_check(self._last_sample)
        if not pressure_ok and self.settings.resource_mode != ResourceMode.MANUAL:
            return running_jobs
        async with self._lock:
            active = dict(self._active_counts)
        # Scheduler tasks move through independently leased stages. Capacity is
        # the largest live stage target, not the minimum: lowering extraction
        # must not permanently cap download concurrency after a job leaves the
        # extraction stage. A task denied its next lease returns durably to
        # WAITING_RESOURCES and is reclaimed on the bounded retry cadence.
        budget = max(limits.values())
        external_extractions = max(
            0, active["extraction"] - max(
                0, running_jobs - sum(active[s] for s in ("download", "merge", "upload"))
            )
        )
        if external_extractions:
            budget = max(
                limits["download"], limits["merge"], limits["upload"],
                max(0, limits["extraction"] - external_extractions),
            )
        return max(running_jobs, budget)

    async def admit_stage(
        self, stage: str, estimated_bytes_needed: int = 0, job_id: Optional[str] = None
    ) -> tuple[bool, str]:
        """Read-only compatibility check; callers should use acquire_stage for a lease."""
        limits = await self.adaptive_limits()
        if self._active_counts.get(stage, 0) >= limits.get(stage, 0):
            return False, f"{stage} concurrency limit reached"
        ok, reason = self._pressure_check(self._last_sample)
        if not ok and self.settings.resource_mode != ResourceMode.MANUAL:
            return False, reason
        if estimated_bytes_needed:
            safety = self.disk_safety_bytes()
            reserved = await self.db.get_total_reserved_bytes(job_id)
            if self.file_mgr.get_free_disk_space() - reserved - safety < estimated_bytes_needed:
                return False, "protected disk headroom would be consumed"
        return True, "Ready"

    def _pressure_check(self, sample: Optional[dict[str, object]]) -> tuple[bool, str]:
        if not sample:
            return True, "Ready"
        if float(sample.get("io_pressure", 0)) >= 10.0:
            return False, "disk I/O is under pressure"
        memory = sample["memory"]
        cpu = sample["cpu"]
        loads = sample["load_average"]
        assert isinstance(memory, dict) and isinstance(cpu, dict) and isinstance(loads, tuple)
        required = int(int(memory["total"]) * self.settings.memory_safety_headroom)
        if int(memory["available"]) <= required:
            return False, "memory safety headroom is under pressure"
        cpu_ceiling = 100 * (1 - self.settings.cpu_safety_headroom)
        if float(cpu["load_percent"]) >= cpu_ceiling:
            return False, "CPU safety headroom is under pressure"
        if loads[0] and float(loads[0]) > float(cpu["cores"]) * 1.25:
            return False, "system load average is under pressure"
        return True, "Ready"

    def current_pressure_reason(self, effective: Optional[dict[str, int]] = None) -> str:
        ok, reason = self._pressure_check(self._last_sample)
        if not ok:
            return reason
        if effective:
            configured = {
                "extraction": self.settings.max_concurrent_extractions,
                "download": self.settings.max_concurrent_downloads,
                "merge": self.settings.max_concurrent_merges,
                "upload": self.settings.max_concurrent_uploads,
            }
            if any(configured[stage] and effective[stage] < configured[stage]
                   for stage in self.STAGES):
                return "adaptive shared-VM safety capacity"
        return "Resources currently healthy"

    def disk_safety_bytes(self) -> int:
        capacity = self.file_mgr.get_disk_capacity()
        fractional = int(capacity * self.settings.disk_safety_headroom_fraction)
        configured = int(self.settings.disk_safety_headroom_gb * 1024**3)
        return max(fractional, configured)

    async def reserve_job_disk(self, job_id: str, requested_bytes: int) -> bool:
        if self.settings.max_job_size_bytes and requested_bytes > self.settings.max_job_size_bytes:
            return False
        return await self.db.reserve_disk_atomic(
            job_id,
            requested_bytes,
            self.file_mgr.get_free_disk_space(),
            self.disk_safety_bytes(),
            self.file_mgr.job_disk_usage(job_id) if self.file_mgr.get_job_dir(job_id).exists() else 0,
        )

    async def growth_is_safe(self, job_id: str, actual_bytes: int) -> bool:
        if self.settings.max_job_size_bytes and actual_bytes > self.settings.max_job_size_bytes:
            return False
        free = self.file_mgr.get_free_disk_space()
        safety = self.disk_safety_bytes()
        usable = max(0, free - safety)
        extension = max(
            8 * 1024**2,
            min(256 * 1024**2, max(1, usable // 4)),
        )
        return await self.db.extend_disk_reservation_atomic(
            job_id,
            actual_bytes,
            free,
            safety,
            extension,
            self.settings.max_job_size_bytes,
        )
