"""Bounded runtime retention and private SQLite backups."""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from datetime import datetime, timezone

from core.config import Settings
from storage.database import Database, ACTIVE_STATUSES
from storage.file_manager import FileManager


class MaintenanceService:
    def __init__(self, db: Database, files: FileManager, settings: Settings):
        self.db, self.files, self.settings = db, files, settings
        self.lock = asyncio.Lock()
        self.last_success = 0.0

    async def clean_orphans(self) -> int:
        removed = 0
        cutoff = time.time() - self.settings.orphan_grace_hours * 3600
        candidates = await asyncio.to_thread(lambda: list(self.files.base_jobs_dir.iterdir()))
        for path in candidates:
            if removed >= 100:
                break
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path.name):
                continue
            try:
                if path.lstat().st_mtime >= cutoff:
                    continue
                job = await self.db.get_job(path.name)
                if job and job.status.value in ACTIVE_STATUSES:
                    continue
                if path.is_dir() or self.files.is_link(path):
                    removed += bool(await asyncio.to_thread(self.files.cleanup_job, path.name))
            except FileNotFoundError:
                continue
        return removed

    async def run(self) -> dict[str, int]:
        async with self.lock:
            result = await self.db.maintain(
                job_days=self.settings.job_history_days,
                cache_days=self.settings.cache_retention_days,
            )
            result["directories"] = await self.clean_orphans()
            self.last_success = time.time()
            return result

    async def backup(self) -> str:
        async with self.lock:
            headroom = max(int(self.files.get_disk_capacity()*self.settings.disk_safety_headroom_fraction),
                           int(self.settings.disk_safety_headroom_gb*1024**3))
            if self.files.get_free_disk_space() - headroom - await self.db.get_total_reserved_bytes() <= self.settings.database_path.stat().st_size * 2:
                raise ValueError("Insufficient protected disk space for backup")
            root = self.settings.data_dir / "backups"
            if self.files.is_link(root):
                raise ValueError("Backup directory must not be a symlink")
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = root.resolve(strict=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"bot-{stamp}-{uuid.uuid4().hex[:8]}.db"
            final = root / name
            partial = root / (name + ".partial")
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                await self.db.backup_to(partial)
                partial.replace(final)
            finally:
                partial.unlink(missing_ok=True)
            backups = sorted((p for p in root.iterdir() if re.fullmatch(
                r"bot-\d{8}-\d{6}-[0-9a-f]{8}\.db", p.name
            ) and p.is_file() and not self.files.is_link(p)), reverse=True)
            for path in backups[self.settings.backup_retention_count:]:
                path.unlink()
            return name
