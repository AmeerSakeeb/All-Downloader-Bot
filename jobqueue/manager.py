"""Persistent job state transitions, ownership-aware cancellation, and recovery."""

from __future__ import annotations

from typing import Optional

from core.exceptions import ErrorCategory
from core.models import JobStatus
from core.models import MediaFormat
from downloads.process_supervisor import ProcessSupervisor
from storage.database import Database
from storage.file_manager import FileManager


class QueueManager:
    def __init__(
        self,
        db: Database,
        file_mgr: FileManager,
        supervisor: Optional[ProcessSupervisor] = None,
    ):
        self.db = db
        self.file_mgr = file_mgr
        self.supervisor = supervisor or ProcessSupervisor()

    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus | str,
        stage_label: str,
        error_message: Optional[str] = None,
    ) -> None:
        job = await self.db.get_job(job_id)
        if not job:
            return
        job.status = status if isinstance(status, JobStatus) else JobStatus(status)
        job.current_stage = stage_label
        job.error_message = error_message
        if job.status != JobStatus.CLAIMED:
            job.claimed_at = None
        await self.db.save_job(job)

    async def cancel_job(self, job_id: str, user_id: Optional[int] = None) -> bool:
        job = await self.db.get_job(job_id)
        if not job or (user_id is not None and job.user_id != user_id):
            return False
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job.status == JobStatus.CANCELLED
        job.status = JobStatus.CANCELLED
        job.current_stage = "Cancelled"
        job.claimed_at = None
        await self.db.save_job(job)
        await self.supervisor.cancel_job(job_id)
        await self.db.release_disk_reservation(job_id)
        self.file_mgr.cleanup_job(job_id)
        return True

    async def reconcile_on_startup(self) -> None:
        """Requeue every recoverable persisted job; never trust stale PIDs."""
        for job in await self.db.get_active_jobs():
            job.process_pid = None
            job.claimed_at = None
            try:
                self.file_mgr.validate_recovery_dir(job.job_id)
                if not job.source_url or not job.video_format_snapshot:
                    raise ValueError("Missing durable execution snapshot")
                MediaFormat.model_validate(job.video_format_snapshot)
                if job.audio_format_id:
                    MediaFormat.model_validate(job.audio_format_snapshot)
                recipients = await self.db.list_job_subscribers(job.job_id, ("waiting", "delivered", "failed", "cancelled"))
                if recipients and not any(r["status"] == "waiting" for r in recipients):
                    if any(r["status"] == "delivered" for r in recipients):
                        job.status = JobStatus.COMPLETED
                        job.error_category = None
                        job.error_message = None
                    elif any(r["status"] == "failed" for r in recipients):
                        job.status = JobStatus.FAILED
                        job.error_category = ErrorCategory.TELEGRAM_DELIVERY_FAILED.value
                        job.error_message = (
                            "Telegram could not deliver the completed media. Please try again."
                        )
                    elif all(r["status"] == "cancelled" for r in recipients):
                        job.status = JobStatus.CANCELLED
                        job.error_category = None
                        job.error_message = None
                    else:
                        job.status = JobStatus.QUEUED
                    job.current_stage = (
                        "Recovered recipient outcome"
                        if job.status in {
                            JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
                        }
                        else "Recovered after restart"
                    )
                else:
                    job.status = JobStatus.QUEUED
                    job.current_stage = "Recovered after restart"
            except Exception:
                job.status = JobStatus.FAILED
                job.current_stage = "Recovery unavailable"
                job.error_category = ErrorCategory.DOWNLOAD_FAILED.value
                job.error_message = "The interrupted download cannot be resumed safely. Send the link again."
            await self.db.save_job(job)
            await self.db.release_disk_reservation(job.job_id)
        await self.db.connection.execute(
            "DELETE FROM disk_reservations WHERE job_id NOT IN (SELECT job_id FROM download_jobs WHERE status='queued')"
        )

    async def prepare_shutdown(self) -> None:
        """Stop owned processes and leave unfinished jobs deterministically recoverable."""
        await self.supervisor.shutdown()
        for job in await self.db.get_active_jobs():
            job.process_pid = None
            job.claimed_at = None
            job.status = JobStatus.QUEUED
            job.current_stage = "Paused for shutdown"
            await self.db.save_job(job)

    async def cancel_all_jobs(self) -> None:
        """Backward-compatible shutdown API that preserves jobs for restart."""
        await self.prepare_shutdown()
