"""Owned subprocess registry with bounded output and graceful tree cancellation."""

from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from security.url_logging import sanitize_log_value


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _BoundedBytes:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.parts: deque[bytes] = deque()
        self.size = 0

    def append(self, data: bytes) -> None:
        self.parts.append(data)
        self.size += len(data)
        while self.size > self.limit and self.parts:
            self.size -= len(self.parts.popleft())

    def value(self) -> bytes:
        return b"".join(self.parts)[-self.limit :]


class ProcessSupervisor:
    """Owns process handles; persisted PIDs are diagnostic only."""

    def __init__(self, terminate_grace_seconds: float = 5.0):
        self.terminate_grace_seconds = terminate_grace_seconds
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return sum(process.returncode is None for process in self._processes.values())

    async def spawn_owned_process(
        self,
        cmd: list[str],
        *,
        job_id: str = "system",
        stage: str = "process",
        **kwargs,
    ) -> asyncio.subprocess.Process:
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000200
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        async with self._lock:
            existing = self._processes.get((job_id, stage))
            if existing and existing.returncode is None:
                await self._terminate_handle(existing)
            self._processes[(job_id, stage)] = process
        return process

    async def run(
        self,
        cmd: list[str],
        *,
        job_id: str = "system",
        stage: str = "process",
        timeout: float,
        max_output_bytes: int = 1024 * 1024,
    ) -> ProcessResult:
        process = await self.spawn_owned_process(
            cmd,
            job_id=job_id,
            stage=stage,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buffer = _BoundedBytes(max_output_bytes)
        stderr_buffer = _BoundedBytes(max_output_bytes)

        async def drain(stream: Optional[asyncio.StreamReader], target: _BoundedBytes) -> None:
            if stream is None:
                return
            while chunk := await stream.read(65536):
                target.append(chunk)

        readers = [
            asyncio.create_task(drain(process.stdout, stdout_buffer)),
            asyncio.create_task(drain(process.stderr, stderr_buffer)),
        ]
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._terminate_handle(process)
                raise
            await asyncio.gather(*readers)
            return ProcessResult(
                process.returncode if process.returncode is not None else -1,
                stdout_buffer.value(),
                stderr_buffer.value(),
            )
        except asyncio.CancelledError:
            # Cancellation of the owner must never orphan a live child. Shield
            # bounded termination/reaping from a second cancellation request.
            termination = asyncio.create_task(self._terminate_handle(process))
            while not termination.done():
                try:
                    await asyncio.shield(termination)
                except asyncio.CancelledError:
                    continue
            await termination
            raise
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await self.unregister(job_id, stage, process)

    async def unregister(
        self, job_id: str, stage: str, process: asyncio.subprocess.Process
    ) -> None:
        async with self._lock:
            if self._processes.get((job_id, stage)) is process:
                self._processes.pop((job_id, stage), None)

    async def cancel_job(self, job_id: str) -> None:
        async with self._lock:
            owned = [
                (key, process)
                for key, process in self._processes.items()
                if key[0] == job_id
            ]
        await asyncio.gather(
            *(self._terminate_handle(process) for _, process in owned),
            return_exceptions=True,
        )
        async with self._lock:
            for key, process in owned:
                if self._processes.get(key) is process:
                    self._processes.pop(key, None)

    async def shutdown(self) -> None:
        async with self._lock:
            processes = list(self._processes.values())
        await asyncio.gather(
            *(self._terminate_handle(process) for process in processes),
            return_exceptions=True,
        )
        async with self._lock:
            self._processes.clear()

    async def _terminate_handle(self, process: asyncio.subprocess.Process) -> int:
        if process.returncode is not None:
            return process.returncode
        self._send_term(process.pid)
        try:
            return await asyncio.wait_for(
                process.wait(), timeout=self.terminate_grace_seconds
            )
        except asyncio.TimeoutError:
            await self.kill_process_tree(process.pid)
            return await process.wait()

    @staticmethod
    def _send_term(pid: int) -> None:
        try:
            if os.name == "nt":
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            else:
                getattr(os, "killpg")(pid, getattr(signal, "SIGTERM"))
        except (ProcessLookupError, PermissionError):
            return

    async def terminate_process_tree(self, pid: int, timeout_secs: float = 3.0) -> None:
        process = next((p for p in self._processes.values() if p.pid == pid), None)
        if process is not None:
            old_grace = self.terminate_grace_seconds
            self.terminate_grace_seconds = timeout_secs
            try:
                await self._terminate_handle(process)
            finally:
                self.terminate_grace_seconds = old_grace
        else:
            self._send_term(pid)

    @staticmethod
    async def kill_process_tree(pid: int) -> None:
        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.wait()
            else:
                getattr(os, "killpg")(pid, getattr(signal, "SIGKILL"))
        except (ProcessLookupError, PermissionError):
            return

    async def wait_and_reap(
        self, process: asyncio.subprocess.Process, timeout_secs: float = 3.0
    ) -> int:
        try:
            return await asyncio.wait_for(process.wait(), timeout_secs)
        except asyncio.TimeoutError:
            await self._terminate_handle(process)
            return process.returncode if process.returncode is not None else -1


def safe_command_summary(command: Iterable[str]) -> str:
    return " ".join(sanitize_log_value(part) for part in command)
