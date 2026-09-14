"""Symlink-safe isolated filesystem management for download jobs."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from core.exceptions import SecurityError

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")


def safe_extension(value: str, fallback: str = "bin") -> str:
    candidate = (value or "").lower().lstrip(".")
    return candidate if _SAFE_EXTENSION.fullmatch(candidate) else fallback


class FileManager:
    def __init__(self, base_jobs_dir: Path):
        if self.is_link(Path(base_jobs_dir)):
            raise SecurityError("Jobs base directory may not be a symlink")
        Path(base_jobs_dir).mkdir(parents=True, exist_ok=True)
        self.base_jobs_dir = Path(base_jobs_dir).resolve(strict=True)
        if self.base_jobs_dir.is_symlink():
            raise SecurityError("Jobs base directory may not be a symlink")

    def _validate_job_id(self, job_id: str) -> None:
        if not _SAFE_COMPONENT.fullmatch(job_id):
            raise SecurityError("Invalid job identifier")

    @staticmethod
    def is_link(path: Path) -> bool:
        try:
            info = path.lstat()
            return stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
        except FileNotFoundError:
            return False

    def validate_recovery_dir(self, job_id: str) -> None:
        directory = self.get_job_dir(job_id, create=False)
        if directory.exists():
            for root, dirs, files in os.walk(directory, followlinks=False):
                if any(self.is_link(Path(root) / name) for name in dirs + files):
                    raise SecurityError("Recovery files contain a forbidden link")

    def get_job_dir(self, job_id: str, *, create: bool = True) -> Path:
        self._validate_job_id(job_id)
        lexical = self.base_jobs_dir / job_id
        if self.is_link(lexical):
            raise SecurityError("Job directory symlink is forbidden")
        if create:
            lexical.mkdir(mode=0o700, parents=False, exist_ok=True)
        resolved = lexical.resolve(strict=create)
        if not resolved.is_relative_to(self.base_jobs_dir):
            raise SecurityError("Job directory escaped the configured storage root")
        return resolved

    def get_job_file_path(self, job_id: str, filename: str) -> Path:
        if not filename or Path(filename).name != filename:
            raise SecurityError("Invalid job filename")
        job_dir = self.get_job_dir(job_id)
        candidate = job_dir / filename
        if self.is_link(candidate):
            raise SecurityError("Output symlinks are forbidden")
        resolved_parent = candidate.parent.resolve(strict=True)
        if resolved_parent != job_dir or not resolved_parent.is_relative_to(self.base_jobs_dir):
            raise SecurityError("Output path escaped the job directory")
        return candidate

    def find_job_file(self, job_id: str, expected_filename: str) -> Path | None:
        """Return a safe regular same-stem result from one isolated job directory."""
        expected = self.get_job_file_path(job_id, expected_filename)
        job_dir = expected.parent.resolve(strict=True)
        if expected.is_file() and not self.is_link(expected):
            return expected
        with os.scandir(job_dir) as entries:
            for entry in entries:
                candidate = job_dir / entry.name
                if (
                    entry.is_file(follow_symlinks=False)
                    and not self.is_link(candidate)
                    and candidate.stem == expected.stem
                    and candidate.suffix != ".part"
                    and candidate.resolve(strict=True).parent == job_dir
                ):
                    return candidate
        return None

    def job_disk_usage(self, job_id: str) -> int:
        job_dir = self.get_job_dir(job_id, create=False)
        total = 0
        with os.scandir(job_dir) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
        return total

    def cleanup_job(self, job_id: str) -> bool:
        self._validate_job_id(job_id)
        lexical = self.base_jobs_dir / job_id
        if not lexical.exists() and not lexical.is_symlink():
            return False
        if self.is_link(lexical):
            if lexical.is_symlink():
                lexical.unlink()
            else:
                lexical.rmdir()
            return True
        resolved = lexical.resolve(strict=True)
        if not resolved.is_relative_to(self.base_jobs_dir) or resolved == self.base_jobs_dir:
            raise SecurityError("Refusing unsafe cleanup target")
        shutil.rmtree(resolved)
        return True

    def get_free_disk_space(self) -> int:
        return shutil.disk_usage(self.base_jobs_dir).free

    def get_disk_capacity(self) -> int:
        return shutil.disk_usage(self.base_jobs_dir).total
