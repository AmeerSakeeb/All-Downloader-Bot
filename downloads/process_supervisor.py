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


class ProcessOutputLimitError(RuntimeError):
    """Raised when semantic subprocess output cannot be retained completely."""

    def __init__(self, stream_name: str, limit: int):
        self.stream_name = stream_name
        self.limit = limit
        super().__init__(f"{stream_name} exceeded the complete-output limit of {limit} bytes")


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


class _CompleteBytes:
    """Bounded prefix buffer that records overflow instead of returning partial data."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.parts: deque[bytes] = deque()
        self.size = 0
        self.overflowed = False

    def append(self, data: bytes) -> None:
        if self.overflowed:
            return
        remaining = self.limit - self.size
        if len(data) > remaining:
            if remaining:
                self.parts.append(data[:remaining])
                self.size += remaining
            self.overflowed = True
            return
        self.parts.append(data)
        self.size += len(data)

    def value(self) -> bytes:
        if self.overflowed:
            raise ProcessOutputLimitError("stdout", self.limit)
        return b"".join(self.parts)


class ProcessSupervisor:
    """Owns process handles; persisted PIDs are diagnostic only."""

    def __init__(self, terminate_grace_seconds: float = 5.0):
        self.terminate_grace_seconds = terminate_grace_seconds
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()
        self._closed = False

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
        kwargs.setdefault("stdin", asyncio.subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000200
        else:
            kwargs["start_new_session"] = True
        async def create_and_register():
            async with self._lock:
                if self._closed:
                    raise RuntimeError("Process supervisor is shutting down")
                existing = self._processes.get((job_id, stage))
                if existing and existing.returncode is None:
                    raise RuntimeError("A process already owns this job stage")
                process = await asyncio.create_subprocess_exec(*cmd, **kwargs)
                self._processes[(job_id, stage)] = process
                return process

        creation = asyncio.create_task(create_and_register())
        try:
            return await asyncio.shield(creation)
        except asyncio.CancelledError:
            # Creation may have reached the OS before cancellation was delivered.
            while not creation.done():
                try:
                    await asyncio.shield(creation)
                except asyncio.CancelledError:
                    continue
            if not creation.cancelled() and creation.exception() is None:
                process = creation.result()
                await self.reap_owned(job_id, stage, process)
            raise

    async def reap_owned(self, job_id: str, stage: str, process) -> None:
        async def finish():
            await self._terminate_handle(process)
            await self.unregister(job_id, stage, process)
        cleanup = asyncio.create_task(finish())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        await cleanup

    async def run(
        self,
        cmd: list[str],
        *,
        job_id: str = "system",
        stage: str = "process",
        timeout: float,
        max_output_bytes: int = 1024 * 1024,
        complete_stdout_limit: Optional[int] = None,
    ) -> ProcessResult:
        process = await self.spawn_owned_process(
            cmd,
            job_id=job_id,
            stage=stage,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buffer = (
            _CompleteBytes(complete_stdout_limit)
            if complete_stdout_limit is not None
            else _BoundedBytes(max_output_bytes)
        )
        stderr_buffer = _BoundedBytes(max_output_bytes)
        stdout_overflow = asyncio.Event()

        async def drain(stream: Optional[asyncio.StreamReader], target) -> None:
            if stream is None:
                return
            while chunk := await stream.read(65536):
                target.append(chunk)
                if isinstance(target, _CompleteBytes) and target.overflowed:
                    stdout_overflow.set()

        readers = [
            asyncio.create_task(drain(process.stdout, stdout_buffer)),
            asyncio.create_task(drain(process.stderr, stderr_buffer)),
        ]
        process_waiter = asyncio.create_task(process.wait())
        overflow_waiter = asyncio.create_task(stdout_overflow.wait())
        try:
            done, _ = await asyncio.wait(
                {process_waiter, overflow_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await self._terminate_handle(process)
                raise asyncio.TimeoutError
            if overflow_waiter in done and stdout_overflow.is_set():
                await self._terminate_handle(process)
                await asyncio.gather(process_waiter, *readers, return_exceptions=True)
                assert isinstance(stdout_buffer, _CompleteBytes)
                raise ProcessOutputLimitError("stdout", stdout_buffer.limit)
            await process_waiter
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
            await self.reap_owned(job_id, stage, process)
            for task in [process_waiter, overflow_waiter, *readers]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                process_waiter, overflow_waiter, *readers, return_exceptions=True
            )
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
            self._closed = True
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
        # Persisted or external PIDs never confer ownership.

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
                    stdin=asyncio.subprocess.DEVNULL,
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
