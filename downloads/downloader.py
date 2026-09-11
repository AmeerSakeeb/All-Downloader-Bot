"""Supervised yt-dlp downloads with bounded diagnostics and live disk protection."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp

from core.config import Settings, get_settings
from core.exceptions import DownloadError, InsufficientDiskSpaceError
from downloads.process_supervisor import ProcessSupervisor
from downloads.progress import ProgressTracker
from storage.file_manager import FileManager

ProgressCallback = Callable[
    [float, int, Optional[int], float, Optional[int]], Awaitable[None] | None
]


class Downloader:
    def __init__(
        self,
        file_mgr: FileManager,
        settings: Optional[Settings] = None,
        *,
        supervisor: Optional[ProcessSupervisor] = None,
        proxy_url: Optional[str] = None,
        growth_check: Optional[Callable[[str, int], Awaitable[bool]]] = None,
    ):
        self.file_mgr = file_mgr
        self.settings = settings or get_settings()
        self.supervisor = supervisor or ProcessSupervisor(
            self.settings.process_terminate_grace_seconds
        )
        self.proxy_url = proxy_url
        self.growth_check = growth_check

    async def download_format(
        self,
        job_id: str,
        url: str,
        format_id: str,
        output_filename: str,
        expected_size: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        pid_callback: Optional[Callable[[int], Awaitable[None] | None]] = None,
        stage: str = "download",
        direct_source: bool = False,
        raw_http: bool = False,
        playlist_index: Optional[int] = None,
    ) -> Path:
        output_path = self.file_mgr.get_job_file_path(job_id, output_filename)
        if raw_http:
            return await self._download_http(
                job_id, url, output_path, expected_size, on_progress
            )
        command = self._build_ytdlp_command(
            url, format_id, output_path, direct_source, playlist_index=playlist_index
        )

        try:
            process = await self.supervisor.spawn_owned_process(
                command,
                job_id=job_id,
                stage=stage,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise DownloadError("yt-dlp is not installed") from error
        if pid_callback:
            await self._maybe_await(pid_callback(process.pid))

        updates: deque[tuple[float, int, Optional[int], float, Optional[int]]] = deque(maxlen=1)
        tracker = ProgressTracker(lambda *args: updates.append(args)) if on_progress else None
        errors: deque[bytes] = deque()
        error_size = 0

        async def read_stdout() -> None:
            if not process.stdout:
                return
            while line := await process.stdout.readline():
                if tracker:
                    tracker.parse_line(line.decode("utf-8", errors="replace"), expected_size)
                    if updates:
                        assert on_progress is not None
                        await self._maybe_await(on_progress(*updates.pop()))

        async def read_stderr() -> None:
            nonlocal error_size
            if not process.stderr:
                return
            while line := await process.stderr.readline():
                errors.append(line)
                error_size += len(line)
                while error_size > 64 * 1024 and errors:
                    error_size -= len(errors.popleft())

        async def monitor_growth() -> None:
            while process.returncode is None:
                await asyncio.sleep(0.5)
                actual = self.file_mgr.job_disk_usage(job_id)
                if self.settings.max_job_size_bytes and actual > self.settings.max_job_size_bytes:
                    await self.supervisor.cancel_job(job_id)
                    raise DownloadError("Download exceeded the configured per-job size limit")
                if self.growth_check and not await self.growth_check(job_id, actual):
                    await self.supervisor.cancel_job(job_id)
                    raise InsufficientDiskSpaceError(actual, self.file_mgr.get_free_disk_space())

        tasks = [
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
            asyncio.create_task(monitor_growth()),
        ]
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=self.settings.download_timeout)
                await asyncio.gather(tasks[0], tasks[1])
                tasks[2].cancel()
                await asyncio.gather(tasks[2], return_exceptions=True)
            except asyncio.TimeoutError as error:
                await self.supervisor.cancel_job(job_id)
                raise DownloadError("Download timed out") from error
            except BaseException:
                await self.supervisor.cancel_job(job_id)
                raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await self.supervisor.unregister(job_id, stage, process)
        for result in results:
            if isinstance(result, (DownloadError, InsufficientDiskSpaceError)):
                raise result
        if process.returncode != 0:
            diagnostic = b"".join(errors).decode("utf-8", errors="replace")[-2000:]
            raise DownloadError(
                f"yt-dlp failed with exit {process.returncode}: {self._redact_diagnostic(diagnostic)}"
            )
        actual_path = self._find_downloaded_file(output_path)
        if not actual_path:
            raise DownloadError("Download completed without a local output file")
        return actual_path

    def _build_ytdlp_command(
        self, url: str, format_id: str, output_path: Path, direct_source: bool,
        *, playlist_index: Optional[int] = None,
    ) -> list[str]:
        command = [
            "yt-dlp",
            "-o",
            str(output_path),
            "--no-warnings",
            "--ignore-config",
            "--no-cookies",
            "--newline",
            "--progress-template",
            "download:__AVDB_PROGRESS__|%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
        ]
        if playlist_index is None:
            command.append("--no-playlist")
        else:
            command.extend(("--playlist-items", str(playlist_index)))
        if not direct_source:
            command[1:1] = ["-f", format_id]
        if self.proxy_url:
            command.extend(("--proxy", self.proxy_url))
        if self.settings.resume_enabled:
            command.append("--continue")
        if self.settings.max_retries > 0:
            command.extend(("--retries", str(self.settings.max_retries), "--fragment-retries", str(self.settings.max_retries)))
        if self.settings.bandwidth_ceiling > 0:
            command.extend(("--limit-rate", str(self.settings.bandwidth_ceiling)))
        command.append(url)
        return command

    async def _download_http(
        self, job_id: str, url: str, output_path: Path,
        expected_size: Optional[int], on_progress: Optional[ProgressCallback],
    ) -> Path:
        """Stream an exact auxiliary source asset through the controlled proxy."""
        part = output_path.with_suffix(output_path.suffix + ".part")
        offset = part.stat().st_size if self.settings.resume_enabled and part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        timeout = aiohttp.ClientTimeout(total=self.settings.download_timeout)
        started = time.monotonic()
        downloaded = offset
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url, headers=headers, proxy=self.proxy_url) as response:
                if response.status >= 400:
                    raise DownloadError(f"Direct source returned HTTP {response.status}")
                append = offset > 0 and response.status == 206
                if not append:
                    offset = downloaded = 0
                length = response.headers.get("Content-Length")
                total = offset + int(length) if length and length.isdigit() else expected_size
                with part.open("ab" if append else "wb") as destination:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        destination.write(chunk)
                        downloaded += len(chunk)
                        if self.settings.max_job_size_bytes and downloaded > self.settings.max_job_size_bytes:
                            raise DownloadError("Download exceeded the configured per-job size limit")
                        if self.growth_check and not await self.growth_check(job_id, downloaded):
                            raise InsufficientDiskSpaceError(downloaded, self.file_mgr.get_free_disk_space())
                        elapsed = max(0.001, time.monotonic() - started)
                        if self.settings.bandwidth_ceiling > 0:
                            expected_elapsed = max(0, downloaded - offset) / self.settings.bandwidth_ceiling
                            if expected_elapsed > elapsed:
                                await asyncio.sleep(expected_elapsed - elapsed)
                                elapsed = max(0.001, time.monotonic() - started)
                        if on_progress:
                            pct = min(100.0, downloaded * 100 / total) if total else 0.0
                            await self._maybe_await(on_progress(
                                pct, downloaded, total, (downloaded - offset) / elapsed,
                                int((total - downloaded) / max(1, (downloaded - offset) / elapsed)) if total else None,
                            ))
        part.replace(output_path)
        return output_path

    @staticmethod
    async def _maybe_await(value) -> None:
        if inspect.isawaitable(value):
            await value

    @staticmethod
    def _redact_diagnostic(value: str) -> str:
        lines = []
        for line in value.splitlines()[-20:]:
            words = ["<url-redacted>" if word.startswith(("http://", "https://")) else word for word in line.split()]
            lines.append(" ".join(words))
        return "\n".join(lines)

    @staticmethod
    def _find_downloaded_file(expected: Path) -> Optional[Path]:
        if expected.is_file() and not expected.is_symlink():
            return expected
        for candidate in expected.parent.iterdir():
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.stem == expected.stem
                and candidate.suffix != ".part"
            ):
                return candidate
        return None
