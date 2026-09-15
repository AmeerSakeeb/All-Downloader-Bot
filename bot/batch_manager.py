"""Managed background task registry for batch analysis."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BatchAnalysisManager:
    """Tracks and manages batch analysis asyncio tasks.

    Supports:
    - start batch analysis (pass a coroutine; manager creates the task)
    - cancel batch analysis (mark cancelled, cancel task, await completion)
    - check if a batch session is cancelled
    - automatic cleanup when tasks finish
    - graceful shutdown (cancel and await all tasks)
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancelled: set[str] = set()

    def start(
        self, session_id: str, coro: Coroutine[Any, Any, Any]
    ) -> asyncio.Task[Any]:
        """Register a batch analysis coroutine as a managed background task.

        The manager internally creates the asyncio.Task with a descriptive name
        and installs a done callback that cleans up the registry entry when the
        task finishes (normally, via failure, or via cancellation).
        """
        current = self._tasks.get(session_id)
        if current is not None and not current.done():
            coro.close()
            raise RuntimeError(f"Batch analysis is already active for {session_id}")

        self._cancelled.discard(session_id)
        task = asyncio.create_task(
            coro,
            name=f"batch-analysis:{session_id}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(
            lambda completed, _sid=session_id: self._on_done(_sid, completed)
        )
        return task

    def _on_done(self, session_id: str, task: asyncio.Task[Any]) -> None:
        """Synchronous callback invoked when a tracked task completes."""
        if not task.cancelled():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None
            if error is not None:
                logger.error(
                    "Managed batch analysis failed for session %s",
                    session_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        # Only the task currently registered under this session may clean its
        # registry state. This prevents a stale done callback from removing a
        # replacement task.
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
            self._cancelled.discard(session_id)

    async def cancel(self, session_id: str) -> None:
        """Mark the batch cancelled, cancel the tracked task, and await it.

        The cancelled marker stays set while the task is still running so that
        publish() can observe it.  After the task is fully reaped the registry
        entry is removed by the done callback.
        """
        self._cancelled.add(session_id)
        task = self._tasks.get(session_id)
        if task is None:
            self._cancelled.discard(session_id)
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Done callbacks normally run while gather is resuming, but make the
        # postcondition explicit: cancel() returns with no retained task or
        # cancelled marker.
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
            self._cancelled.discard(session_id)

    def is_cancelled(self, session_id: str) -> bool:
        return session_id in self._cancelled

    def has_task(self, session_id: str) -> bool:
        """Return True if a batch task is still tracked (not yet cleaned up)."""
        return session_id in self._tasks

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def cleanup(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)
        self._cancelled.discard(session_id)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._cancelled.clear()

    async def await_completion(self, session_id: str) -> None:
        """Wait for a tracked batch task to finish if one exists."""
        task = self._tasks.get(session_id)
        if task is None:
            return
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# Module-level singleton shared across all batch analysis operations.
global_batch_manager = BatchAnalysisManager()
