"""Async SQLite persistence, ordered migrations, and atomic job operations."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from core.models import (
    DetailStyle, DownloadJob, FavoriteFormatRule, JobStatus, MatchingStrategy,
    MediaFormat, MediaSession, UserSettings,
)

CURRENT_SCHEMA_VERSION = 4
ACTIVE_STATUSES = tuple(
    status.value
    for status in (
        JobStatus.QUEUED,
        JobStatus.CLAIMED,
        JobStatus.WAITING_RESOURCES,
        JobStatus.DOWNLOADING_VIDEO,
        JobStatus.DOWNLOADING_AUDIO,
        JobStatus.MERGING,
        JobStatus.UPLOADING,
    )
)


class QueueLimitError(ValueError):
    """Safe rejection message for a full private-bot queue."""


class Database:
    """A single-connection SQLite store with short, serialized write transactions."""

    def __init__(self, db_path: Path, default_send_mode: str = "document"):
        self.db_path = Path(db_path)
        self.default_send_mode = default_send_mode
        self._db: Optional[aiosqlite.Connection] = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected")
        return self._db

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path), isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        try:
            await self._run_migrations()
        except BaseException:
            await self._db.close()
            self._db = None
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def ping(self) -> bool:
        row = await (await self.connection.execute("SELECT 1 AS ok")).fetchone()
        return bool(row and row["ok"] == 1)

    async def _run_migrations(self) -> None:
        db = self.connection
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        row = await (
            await db.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations")
        ).fetchone()
        assert row is not None
        current = int(row["version"])
        migrations = {
            1: self._migration_v1,
            2: self._migration_v2,
            3: self._migration_v3,
            4: self._migration_v4,
        }
        for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            await db.execute("BEGIN IMMEDIATE")
            try:
                await migrations[version]()
                await db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _migration_v1(self) -> None:
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_allowed INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT,
                extractor TEXT NOT NULL,
                title TEXT,
                duration INTEGER,
                uploader TEXT,
                thumbnail_url TEXT,
                formats_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS download_jobs (
                job_id TEXT PRIMARY KEY,
                media_session_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                video_format_id TEXT NOT NULL,
                audio_format_id TEXT,
                status TEXT NOT NULL,
                progress_pct REAL NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER,
                speed_bytes_sec REAL NOT NULL DEFAULT 0,
                eta_seconds INTEGER,
                current_stage TEXT NOT NULL,
                process_pid INTEGER,
                error_message TEXT,
                error_category TEXT,
                send_mode TEXT NOT NULL DEFAULT 'document',
                output_path TEXT,
                telegram_file_id TEXT,
                snapshot_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS disk_reservations (
                job_id TEXT PRIMARY KEY,
                reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
                created_at REAL NOT NULL,
                FOREIGN KEY(job_id) REFERENCES download_jobs(job_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON download_jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON media_sessions(expires_at);
            """
        )

    async def _migration_v2(self) -> None:
        rows = await (
            await self.connection.execute("PRAGMA table_info(download_jobs)")
        ).fetchall()
        columns = {row["name"] for row in rows}
        for name, sql_type in (
            ("source_url", "TEXT"),
            ("canonical_url", "TEXT"),
            ("extractor", "TEXT"),
            ("claimed_at", "REAL"),
        ):
            if name not in columns:
                await self.connection.execute(
                    f"ALTER TABLE download_jobs ADD COLUMN {name} {sql_type}"
                )

    async def _migration_v3(self) -> None:
        await self.connection.execute(
            """CREATE TABLE IF NOT EXISTS disk_reservations (
                job_id TEXT PRIMARY KEY,
                reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes >= 0),
                created_at REAL NOT NULL
            )"""
        )
        rows = await (
            await self.connection.execute("PRAGMA table_info(disk_reservations)")
        ).fetchall()
        columns = {row["name"] for row in rows}
        if "projected_bytes" not in columns:
            await self.connection.execute(
                "ALTER TABLE disk_reservations ADD COLUMN projected_bytes INTEGER NOT NULL DEFAULT 0"
            )
            await self.connection.execute(
                "UPDATE disk_reservations SET projected_bytes=reserved_bytes"
            )
        if "actual_bytes" not in columns:
            await self.connection.execute(
                "ALTER TABLE disk_reservations ADD COLUMN actual_bytes INTEGER NOT NULL DEFAULT 0"
            )

    async def _migration_v4(self) -> None:
        # Be tolerant of early/minimal Phase 1 databases while preserving all
        # existing session rows in normal upgrades.
        await self.connection.execute(
            """CREATE TABLE IF NOT EXISTS media_sessions (
                session_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,url TEXT NOT NULL,
                canonical_url TEXT,extractor TEXT NOT NULL,title TEXT,duration INTEGER,
                uploader TEXT,thumbnail_url TEXT,formats_json TEXT NOT NULL,
                created_at REAL NOT NULL,expires_at REAL NOT NULL
            )"""
        )
        session_columns = {
            row["name"]
            for row in await (await self.connection.execute("PRAGMA table_info(media_sessions)")).fetchall()
        }
        if "extras_json" not in session_columns:
            await self.connection.execute(
                "ALTER TABLE media_sessions ADD COLUMN extras_json TEXT NOT NULL DEFAULT '{}'"
            )
        job_columns = {
            row["name"]
            for row in await (await self.connection.execute("PRAGMA table_info(download_jobs)")).fetchall()
        }
        for name, sql_type in (
            ("media_kind", "TEXT NOT NULL DEFAULT 'video'"),
            ("output_identity", "TEXT"),
        ):
            if name not in job_columns:
                await self.connection.execute(
                    f"ALTER TABLE download_jobs ADD COLUMN {name} {sql_type}"
                )
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                send_mode TEXT NOT NULL DEFAULT 'document',
                matching_strategy TEXT NOT NULL DEFAULT 'best_quality',
                detail_style TEXT NOT NULL DEFAULT 'rich',
                automatic_audio INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS favorite_format_rules (
                rule_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                priority INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                criteria_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_favorite_rules_user
                ON favorite_format_rules(user_id,priority);
            CREATE TABLE IF NOT EXISTS ui_drafts (
                user_id INTEGER PRIMARY KEY,
                draft_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_file_cache (
                output_identity TEXT PRIMARY KEY,
                telegram_file_id TEXT NOT NULL,
                send_mode TEXT NOT NULL,
                output_container TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS job_subscribers (
                subscriber_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                send_mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at REAL NOT NULL,
                UNIQUE(job_id,user_id,chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_subscribers_job ON job_subscribers(job_id,status);
            CREATE INDEX IF NOT EXISTS idx_jobs_output_identity ON download_jobs(output_identity,status);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_output_identity
                ON download_jobs(output_identity)
                WHERE output_identity IS NOT NULL AND status IN
                ('queued','claimed','waiting_resources','downloading_video',
                 'downloading_audio','merging','uploading');
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )

    async def schema_version(self) -> int:
        row = await (
            await self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            )
        ).fetchone()
        assert row is not None
        return int(row["version"])

    async def bootstrap_admins(self, user_ids: Iterable[int]) -> None:
        for user_id in set(int(value) for value in user_ids):
            await self.add_or_update_user(user_id, is_admin=True, is_allowed=True)

    async def add_or_update_user(
        self, user_id: int, *, is_admin: bool = False, is_allowed: bool = True
    ) -> None:
        now = time.time()
        await self.connection.execute(
            """INSERT INTO users(user_id,is_admin,is_allowed,created_at,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               is_admin=excluded.is_admin,is_allowed=excluded.is_allowed,
               updated_at=excluded.updated_at""",
            (int(user_id), int(is_admin), int(is_allowed), now, now),
        )

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        row = await (
            await self.connection.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),))
        ).fetchone()
        return self._user_dict(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await (
            await self.connection.execute(
                "SELECT * FROM users ORDER BY is_admin DESC,user_id"
            )
        ).fetchall()
        return [self._user_dict(row) for row in rows]

    @staticmethod
    def _user_dict(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        data["is_admin"] = bool(data["is_admin"])
        data["is_allowed"] = bool(data["is_allowed"])
        return data

    async def save_media_session(self, session: MediaSession) -> None:
        extras = session.model_dump(
            mode="json",
            include={
                "description", "upload_date", "media_id", "session_kind",
                "collection_truncated", "thumbnails", "subtitles", "items",
            },
        )
        await self.connection.execute(
            """INSERT OR REPLACE INTO media_sessions
               (session_id,user_id,url,canonical_url,extractor,title,duration,uploader,
                thumbnail_url,formats_json,created_at,expires_at,extras_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session.session_id,
                session.user_id,
                session.url,
                session.canonical_url,
                session.extractor,
                session.title,
                session.duration,
                session.uploader,
                session.thumbnail_url,
                json.dumps([item.model_dump(mode="json") for item in session.formats]),
                session.created_at,
                session.expires_at,
                json.dumps(extras, separators=(",", ":")),
            ),
        )

    async def get_media_session(self, session_id: str) -> Optional[MediaSession]:
        row = await (
            await self.connection.execute(
                "SELECT * FROM media_sessions WHERE session_id=?", (session_id,)
            )
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["formats"] = json.loads(data.pop("formats_json"))
        data.update(json.loads(data.pop("extras_json", "{}") or "{}"))
        return MediaSession.model_validate(data)

    async def delete_media_session(
        self, session_id: str, user_id: Optional[int] = None
    ) -> bool:
        sql = "DELETE FROM media_sessions WHERE session_id=?"
        params: list[Any] = [session_id]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        cursor = await self.connection.execute(sql, params)
        return cursor.rowcount > 0

    async def delete_expired_sessions(self, now: Optional[float] = None) -> int:
        """Retain legacy sessions only where an active job lacks an execution snapshot."""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        cursor = await self.connection.execute(
            f"""DELETE FROM media_sessions AS s WHERE s.expires_at < ? AND NOT EXISTS (
                    SELECT 1 FROM download_jobs AS j WHERE j.media_session_id=s.session_id
                    AND j.status IN ({placeholders})
                    AND (j.snapshot_json IS NULL OR j.snapshot_json='')
                )""",
            (now or time.time(), *ACTIVE_STATUSES),
        )
        return cursor.rowcount

    async def create_download_job(
        self,
        *,
        session: MediaSession,
        video_format: MediaFormat,
        audio_format: Optional[MediaFormat],
        chat_id: int,
        message_id: Optional[int] = None,
        send_mode: str = "document",
        media_kind: str = "video",
        output_identity: Optional[str] = None,
        max_queued_jobs_per_user: Optional[int] = None,
        max_total_queued_jobs: Optional[int] = None,
    ) -> DownloadJob:
        """Validate selection and persist an independently executable job atomically."""
        if session.is_expired():
            raise ValueError("Media session has expired")
        if session.get_format_by_key(video_format.internal_key) is None:
            raise ValueError("Selected format is not part of this session")
        if audio_format and session.get_format_by_key(audio_format.internal_key) is None:
            raise ValueError("Selected audio is not part of this session")
        video_snapshot: dict[str, Any] = video_format.execution_snapshot()
        audio_snapshot: Optional[dict[str, Any]] = (
            audio_format.execution_snapshot() if audio_format else None
        )
        snapshot: dict[str, Any] = {
            "video": video_snapshot,
            "audio": audio_snapshot,
            "source_url": session.url,
            "canonical_url": session.canonical_url,
            "extractor": session.extractor,
            "title": session.title,
        }
        job = DownloadJob(
            media_session_id=session.session_id,
            user_id=session.user_id,
            chat_id=chat_id,
            message_id=message_id,
            video_format_id=video_format.format_id,
            audio_format_id=audio_format.format_id if audio_format else None,
            video_format_snapshot=video_snapshot,
            audio_format_snapshot=audio_snapshot,
            source_url=session.url,
            canonical_url=session.canonical_url,
            extractor=session.extractor,
            send_mode=send_mode,
            media_kind=media_kind,
            output_identity=output_identity,
        )
        # A separate connection isolates this short transaction from concurrent
        # progress/reservation writes on the application's main connection.
        db = await aiosqlite.connect(str(self.db_path), isolation_level=None)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            if (
                max_queued_jobs_per_user is not None
                and await self.count_active_jobs_for_user(session.user_id, connection=db)
                >= max_queued_jobs_per_user
            ):
                raise QueueLimitError(
                    "You already have the maximum number of active/queued downloads."
                )
            if (
                max_total_queued_jobs is not None
                and await self.count_total_active_jobs(connection=db) >= max_total_queued_jobs
            ):
                raise QueueLimitError("The server queue is currently full.")
            live = await (
                await db.execute(
                    "SELECT user_id,expires_at FROM media_sessions WHERE session_id=?",
                    (session.session_id,),
                )
            ).fetchone()
            if (
                not live
                or int(live["user_id"]) != session.user_id
                or float(live["expires_at"]) < time.time()
            ):
                raise ValueError("Media session is stale or not owned by this user")
            await self._insert_job(job, snapshot, connection=db)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.close()
        return job

    async def _insert_job(
        self, job: DownloadJob, snapshot: dict[str, Any],
        *, connection: Optional[aiosqlite.Connection] = None,
    ) -> None:
        await (connection or self.connection).execute(
            """INSERT INTO download_jobs
               (job_id,media_session_id,user_id,chat_id,message_id,video_format_id,
                audio_format_id,status,progress_pct,downloaded_bytes,total_bytes,
                speed_bytes_sec,eta_seconds,current_stage,process_pid,error_message,
                error_category,send_mode,output_path,telegram_file_id,snapshot_json,
                created_at,updated_at,source_url,canonical_url,extractor,claimed_at,
                media_kind,output_identity)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            self._job_values(job, snapshot),
        )

    def _job_values(
        self, job: DownloadJob, snapshot: Optional[dict[str, Any]] = None
    ) -> tuple[Any, ...]:
        snapshot = snapshot or {
            "video": job.video_format_snapshot,
            "audio": job.audio_format_snapshot,
            "source_url": job.source_url,
            "canonical_url": job.canonical_url,
            "extractor": job.extractor,
        }
        return (
            job.job_id,
            job.media_session_id,
            job.user_id,
            job.chat_id,
            job.message_id,
            job.video_format_id,
            job.audio_format_id,
            job.status.value,
            job.progress_pct,
            job.downloaded_bytes,
            job.total_bytes,
            job.speed_bytes_sec,
            job.eta_seconds,
            job.current_stage,
            job.process_pid,
            job.error_message,
            job.error_category,
            job.send_mode,
            job.output_path,
            job.telegram_file_id,
            json.dumps(snapshot, separators=(",", ":")),
            job.created_at,
            time.time(),
            job.source_url,
            job.canonical_url,
            job.extractor,
            job.claimed_at,
            job.media_kind,
            job.output_identity,
        )

    async def save_job(self, job: DownloadJob) -> None:
        job.updated_at = time.time()
        snapshot = {
            "video": job.video_format_snapshot,
            "audio": job.audio_format_snapshot,
            "source_url": job.source_url,
            "canonical_url": job.canonical_url,
            "extractor": job.extractor,
        }
        await self.connection.execute(
            """UPDATE download_jobs SET message_id=?,status=?,progress_pct=?,
               downloaded_bytes=?,total_bytes=?,speed_bytes_sec=?,eta_seconds=?,
               current_stage=?,process_pid=?,error_message=?,error_category=?,send_mode=?,
               output_path=?,telegram_file_id=?,snapshot_json=?,updated_at=?,source_url=?,
               canonical_url=?,extractor=?,claimed_at=?,media_kind=?,output_identity=?
               WHERE job_id=? AND (status<>? OR ?=?)""",
            (
                job.message_id,
                job.status.value,
                job.progress_pct,
                job.downloaded_bytes,
                job.total_bytes,
                job.speed_bytes_sec,
                job.eta_seconds,
                job.current_stage,
                job.process_pid,
                job.error_message,
                job.error_category,
                job.send_mode,
                job.output_path,
                job.telegram_file_id,
                json.dumps(snapshot, separators=(",", ":")),
                job.updated_at,
                job.source_url,
                job.canonical_url,
                job.extractor,
                job.claimed_at,
                job.media_kind,
                job.output_identity,
                job.job_id,
                JobStatus.CANCELLED.value,
                job.status.value,
                JobStatus.CANCELLED.value,
            ),
        )

    async def get_job(self, job_id: str) -> Optional[DownloadJob]:
        row = await (
            await self.connection.execute(
                "SELECT * FROM download_jobs WHERE job_id=?", (job_id,)
            )
        ).fetchone()
        return self._job_from_row(row) if row else None

    async def get_jobs_by_status(
        self, status: JobStatus | str, limit: int = 100
    ) -> list[DownloadJob]:
        value = status.value if isinstance(status, JobStatus) else str(status)
        rows = await (
            await self.connection.execute(
                "SELECT * FROM download_jobs WHERE status=? ORDER BY created_at LIMIT ?",
                (value, limit),
            )
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    async def list_queued_jobs(self, limit: int = 100) -> list[DownloadJob]:
        rows = await (
            await self.connection.execute(
                "SELECT * FROM download_jobs WHERE status IN (?,?) "
                "ORDER BY updated_at,created_at LIMIT ?",
                (JobStatus.QUEUED.value, JobStatus.WAITING_RESOURCES.value, limit),
            )
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    async def count_active_jobs_for_user(
        self, user_id: int, *, connection: Optional[aiosqlite.Connection] = None
    ) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = await (
            await (connection or self.connection).execute(
                f"""SELECT COUNT(*) AS active_count FROM (
                    SELECT job_id AS token FROM download_jobs
                    WHERE user_id=? AND status IN ({marks})
                    UNION ALL
                    SELECT s.subscriber_id AS token FROM job_subscribers AS s
                    JOIN download_jobs AS j ON j.job_id=s.job_id
                    WHERE s.user_id=? AND s.status='waiting' AND j.status IN ({marks})
                )""",
                (int(user_id), *ACTIVE_STATUSES, int(user_id), *ACTIVE_STATUSES),
            )
        ).fetchone()
        assert row is not None
        return int(row["active_count"])

    async def count_total_active_jobs(
        self, *, connection: Optional[aiosqlite.Connection] = None
    ) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = await (
            await (connection or self.connection).execute(
                f"SELECT COUNT(*) as active_count FROM download_jobs WHERE status IN ({marks})",
                ACTIVE_STATUSES,
            )
        ).fetchone()
        assert row is not None
        return int(row["active_count"])

    async def get_active_jobs(self) -> list[DownloadJob]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = await (
            await self.connection.execute(
                f"SELECT * FROM download_jobs WHERE status IN ({marks}) ORDER BY created_at",
                ACTIVE_STATUSES,
            )
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    async def claim_job(self, job_id: str) -> bool:
        now = time.time()
        cursor = await self.connection.execute(
            "UPDATE download_jobs SET status=?,claimed_at=?,updated_at=? "
            "WHERE job_id=? AND status IN (?,?)",
            (
                JobStatus.CLAIMED.value,
                now,
                now,
                job_id,
                JobStatus.QUEUED.value,
                JobStatus.WAITING_RESOURCES.value,
            ),
        )
        return cursor.rowcount == 1

    async def reserve_disk_atomic(
        self, job_id: str, requested_bytes: int, free_bytes: int, safety_bytes: int,
        actual_bytes: int = 0,
    ) -> bool:
        """Serialize the capacity check and reservation without holding it during I/O."""
        requested_bytes = max(0, int(requested_bytes))
        db = await aiosqlite.connect(str(self.db_path), isolation_level=None)
        db.row_factory = aiosqlite.Row
        transaction_started = False
        try:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            transaction_started = True
            row = await (
                await db.execute(
                    "SELECT COALESCE(SUM(reserved_bytes),0) AS total "
                    "FROM disk_reservations WHERE job_id<>?",
                    (job_id,),
                )
            ).fetchone()
            assert row is not None
            remaining = max(0, requested_bytes - max(0, int(actual_bytes)))
            available = max(
                0,
                int(free_bytes) - int(row["total"]) - max(0, int(safety_bytes)),
            )
            if remaining > available:
                await db.rollback()
                transaction_started = False
                return False
            await db.execute(
                "INSERT INTO disk_reservations"
                "(job_id,reserved_bytes,created_at,projected_bytes,actual_bytes) "
                "VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "reserved_bytes=excluded.reserved_bytes,created_at=excluded.created_at,"
                "projected_bytes=excluded.projected_bytes,actual_bytes=excluded.actual_bytes",
                (job_id, remaining, time.time(), requested_bytes, actual_bytes),
            )
            await db.commit()
            transaction_started = False
            return True
        except BaseException:
            if transaction_started:
                await db.rollback()
            raise
        finally:
            await db.close()

    async def create_disk_reservation(self, job_id: str, reserved_bytes: int) -> None:
        await self.connection.execute(
            "INSERT INTO disk_reservations"
            "(job_id,reserved_bytes,created_at,projected_bytes,actual_bytes) VALUES(?,?,?,?,0) "
            "ON CONFLICT(job_id) DO UPDATE SET reserved_bytes=excluded.reserved_bytes,"
            "created_at=excluded.created_at,projected_bytes=excluded.projected_bytes",
            (job_id, max(0, int(reserved_bytes)), time.time(), max(0, int(reserved_bytes))),
        )

    async def update_disk_reservation(self, job_id: str, reserved_bytes: int) -> None:
        await self.create_disk_reservation(job_id, reserved_bytes)

    async def release_disk_reservation(self, job_id: str) -> None:
        await self.connection.execute(
            "DELETE FROM disk_reservations WHERE job_id=?", (job_id,)
        )

    async def update_disk_growth(self, job_id: str, actual_bytes: int) -> bool:
        """Convert projected reservation to actual usage without double counting."""
        row = await (
            await self.connection.execute(
                "SELECT projected_bytes FROM disk_reservations WHERE job_id=?", (job_id,)
            )
        ).fetchone()
        if not row:
            return False
        projected = int(row["projected_bytes"])
        actual = max(0, int(actual_bytes))
        await self.connection.execute(
            "UPDATE disk_reservations SET actual_bytes=?,reserved_bytes=? WHERE job_id=?",
            (actual, max(0, projected - actual), job_id),
        )
        return actual <= projected

    async def get_total_reserved_bytes(
        self, exclude_job_id: Optional[str] = None
    ) -> int:
        if exclude_job_id:
            row = await (
                await self.connection.execute(
                    "SELECT COALESCE(SUM(reserved_bytes),0) AS total "
                    "FROM disk_reservations WHERE job_id<>?",
                    (exclude_job_id,),
                )
            ).fetchone()
        else:
            row = await (
                await self.connection.execute(
                    "SELECT COALESCE(SUM(reserved_bytes),0) AS total FROM disk_reservations"
                )
            ).fetchone()
        assert row is not None
        return int(row["total"])

    async def get_user_settings(self, user_id: int) -> UserSettings:
        row = await (
            await self.connection.execute(
                "SELECT * FROM user_settings WHERE user_id=?", (int(user_id),)
            )
        ).fetchone()
        if not row:
            settings = UserSettings(user_id=int(user_id), send_mode=self.default_send_mode)
            await self.save_user_settings(settings)
            return settings
        return UserSettings.model_validate(dict(row))

    async def save_user_settings(self, settings: UserSettings) -> None:
        await self.connection.execute(
            """INSERT INTO user_settings
               (user_id,send_mode,matching_strategy,detail_style,automatic_audio,updated_at)
               VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
               send_mode=excluded.send_mode,matching_strategy=excluded.matching_strategy,
               detail_style=excluded.detail_style,automatic_audio=excluded.automatic_audio,
               updated_at=excluded.updated_at""",
            (
                settings.user_id,
                settings.send_mode,
                settings.matching_strategy.value,
                settings.detail_style.value,
                int(settings.automatic_audio),
                time.time(),
            ),
        )

    async def list_favorite_rules(self, user_id: int) -> list[FavoriteFormatRule]:
        rows = await (
            await self.connection.execute(
                "SELECT * FROM favorite_format_rules WHERE user_id=? ORDER BY priority,created_at",
                (int(user_id),),
            )
        ).fetchall()
        result: list[FavoriteFormatRule] = []
        for row in rows:
            data = json.loads(row["criteria_json"])
            data.update(
                rule_id=row["rule_id"], user_id=row["user_id"], name=row["name"],
                priority=row["priority"], enabled=bool(row["enabled"]),
            )
            result.append(FavoriteFormatRule.model_validate(data))
        return result

    async def ensure_default_favorite_rules(self, user_id: int) -> list[FavoriteFormatRule]:
        rules = await self.list_favorite_rules(user_id)
        if rules:
            return rules
        rule = FavoriteFormatRule(
            user_id=user_id,
            name="Best available",
            codecs=["any"], resolutions=["any"], fps_values=["best"], containers=["any"],
        )
        await self.save_favorite_rule(rule)
        return [rule]

    async def save_favorite_rule(self, rule: FavoriteFormatRule) -> None:
        criteria = rule.model_dump(
            mode="json", include={"codecs", "resolutions", "fps_values", "containers"}
        )
        now = time.time()
        await self.connection.execute(
            """INSERT INTO favorite_format_rules
               (rule_id,user_id,name,priority,enabled,criteria_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET
               name=excluded.name,priority=excluded.priority,enabled=excluded.enabled,
               criteria_json=excluded.criteria_json,updated_at=excluded.updated_at
               WHERE favorite_format_rules.user_id=excluded.user_id""",
            (
                rule.rule_id, rule.user_id, rule.name, rule.priority, int(rule.enabled),
                json.dumps(criteria, separators=(",", ":")), now, now,
            ),
        )

    async def get_favorite_rule(
        self, rule_id: str, user_id: int
    ) -> Optional[FavoriteFormatRule]:
        return next(
            (rule for rule in await self.list_favorite_rules(user_id) if rule.rule_id == rule_id),
            None,
        )

    async def delete_favorite_rule(self, rule_id: str, user_id: int) -> bool:
        cursor = await self.connection.execute(
            "DELETE FROM favorite_format_rules WHERE rule_id=? AND user_id=?",
            (rule_id, int(user_id)),
        )
        return cursor.rowcount == 1

    async def duplicate_favorite_rule(
        self, rule_id: str, user_id: int
    ) -> Optional[FavoriteFormatRule]:
        source = await self.get_favorite_rule(rule_id, user_id)
        if not source:
            return None
        rules = await self.list_favorite_rules(user_id)
        duplicate = source.model_copy(
            update={
                "rule_id": uuid.uuid4().hex[:12],
                "name": f"{source.name} copy",
                "priority": len(rules),
            }
        )
        await self.save_favorite_rule(duplicate)
        return duplicate

    async def reorder_favorite_rule(self, rule_id: str, user_id: int, delta: int) -> bool:
        rules = await self.list_favorite_rules(user_id)
        index = next((i for i, rule in enumerate(rules) if rule.rule_id == rule_id), None)
        if index is None:
            return False
        target = max(0, min(len(rules) - 1, index + delta))
        if target == index:
            return True
        rules[index], rules[target] = rules[target], rules[index]
        for priority, rule in enumerate(rules):
            await self.save_favorite_rule(rule.model_copy(update={"priority": priority}))
        return True

    async def save_ui_draft(self, user_id: int, draft: dict[str, Any]) -> None:
        await self.connection.execute(
            """INSERT INTO ui_drafts(user_id,draft_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET draft_json=excluded.draft_json,
               updated_at=excluded.updated_at""",
            (int(user_id), json.dumps(draft, separators=(",", ":")), time.time()),
        )

    async def get_ui_draft(self, user_id: int) -> Optional[dict[str, Any]]:
        row = await (
            await self.connection.execute(
                "SELECT draft_json FROM ui_drafts WHERE user_id=?", (int(user_id),)
            )
        ).fetchone()
        return json.loads(row["draft_json"]) if row else None

    async def delete_ui_draft(self, user_id: int) -> None:
        await self.connection.execute("DELETE FROM ui_drafts WHERE user_id=?", (int(user_id),))

    async def get_cached_file(self, output_identity: str) -> Optional[dict[str, Any]]:
        row = await (
            await self.connection.execute(
                "SELECT * FROM telegram_file_cache WHERE output_identity=?", (output_identity,)
            )
        ).fetchone()
        if not row:
            return None
        await self.connection.execute(
            "UPDATE telegram_file_cache SET last_used_at=?,hit_count=hit_count+1 WHERE output_identity=?",
            (time.time(), output_identity),
        )
        return dict(row)

    async def save_cached_file(
        self, output_identity: str, file_id: str, send_mode: str, output_container: str
    ) -> None:
        now = time.time()
        await self.connection.execute(
            """INSERT INTO telegram_file_cache
               (output_identity,telegram_file_id,send_mode,output_container,created_at,last_used_at,hit_count)
               VALUES(?,?,?,?,?,?,0) ON CONFLICT(output_identity) DO UPDATE SET
               telegram_file_id=excluded.telegram_file_id,send_mode=excluded.send_mode,
               output_container=excluded.output_container,last_used_at=excluded.last_used_at""",
            (output_identity, file_id, send_mode, output_container, now, now),
        )

    async def invalidate_cached_file(self, output_identity: str) -> None:
        await self.connection.execute(
            "DELETE FROM telegram_file_cache WHERE output_identity=?", (output_identity,)
        )

    async def find_active_job_by_identity(self, output_identity: str) -> Optional[DownloadJob]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = await (
            await self.connection.execute(
                f"SELECT * FROM download_jobs WHERE output_identity=? AND status IN ({marks}) "
                "ORDER BY created_at LIMIT 1",
                (output_identity, *ACTIVE_STATUSES),
            )
        ).fetchone()
        return self._job_from_row(row) if row else None

    async def add_job_subscriber(
        self,
        job_id: str,
        user_id: int,
        chat_id: int,
        message_id: Optional[int],
        send_mode: str,
        max_queued_jobs_per_user: int,
    ) -> str:
        db = await aiosqlite.connect(str(self.db_path), isolation_level=None)
        db.row_factory = aiosqlite.Row
        subscriber_id = uuid.uuid4().hex
        try:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            if await self.count_active_jobs_for_user(user_id, connection=db) >= max_queued_jobs_per_user:
                raise QueueLimitError(
                    "You already have the maximum number of active/queued downloads."
                )
            await db.execute(
                """INSERT INTO job_subscribers
                   (subscriber_id,job_id,user_id,chat_id,message_id,send_mode,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (subscriber_id, job_id, user_id, chat_id, message_id, send_mode, "waiting", time.time()),
            )
            await db.commit()
            return subscriber_id
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def list_job_subscribers(self, job_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self.connection.execute(
                "SELECT * FROM job_subscribers WHERE job_id=? AND status='waiting' ORDER BY created_at",
                (job_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def mark_subscriber(self, subscriber_id: str, status: str) -> None:
        await self.connection.execute(
            "UPDATE job_subscribers SET status=? WHERE subscriber_id=?", (status, subscriber_id)
        )

    async def cancel_subscriber(self, subscriber_id: str, user_id: int) -> bool:
        cursor = await self.connection.execute(
            "UPDATE job_subscribers SET status='cancelled' "
            "WHERE subscriber_id=? AND user_id=? AND status='waiting'",
            (subscriber_id, int(user_id)),
        )
        return cursor.rowcount == 1

    async def set_system_setting(self, key: str, value: str) -> None:
        await self.connection.execute(
            """INSERT INTO system_settings(setting_key,setting_value,updated_at)
               VALUES(?,?,?) ON CONFLICT(setting_key) DO UPDATE SET
               setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            (key, value, time.time()),
        )

    async def get_system_setting(self, key: str) -> Optional[str]:
        row = await (
            await self.connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key=?", (key,)
            )
        ).fetchone()
        return str(row["setting_value"]) if row else None

    async def phase2_stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, sql in {
            "active": f"SELECT COUNT(*) FROM download_jobs WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
            "waiting": "SELECT COUNT(*) FROM download_jobs WHERE status='waiting_resources'",
            "failed": "SELECT COUNT(*) FROM download_jobs WHERE status='failed'",
            "successful": "SELECT COUNT(*) FROM download_jobs WHERE status='completed'",
            "cache_hits": "SELECT COALESCE(SUM(hit_count),0) FROM telegram_file_cache",
        }.items():
            params = ACTIVE_STATUSES if key == "active" else ()
            row = await (await self.connection.execute(sql, params)).fetchone()
            result[key] = int(row[0]) if row else 0
        return result

    async def user_job_stats(self, user_id: int) -> dict[str, int]:
        rows = await (
            await self.connection.execute(
                "SELECT status,COUNT(*) AS total FROM download_jobs WHERE user_id=? GROUP BY status",
                (int(user_id),),
            )
        ).fetchall()
        values = {str(row["status"]): int(row["total"]) for row in rows}
        return {
            "total": sum(values.values()),
            "completed": values.get("completed", 0),
            "failed": values.get("failed", 0),
            "active": sum(values.get(status, 0) for status in ACTIVE_STATUSES),
        }

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> DownloadJob:
        data = dict(row)
        snapshot = json.loads(data.pop("snapshot_json") or "{}")
        data["video_format_snapshot"] = snapshot.get("video") or {}
        data["audio_format_snapshot"] = snapshot.get("audio")
        data["source_url"] = data.get("source_url") or snapshot.get("source_url")
        data["canonical_url"] = data.get("canonical_url") or snapshot.get("canonical_url")
        data["extractor"] = data.get("extractor") or snapshot.get("extractor")
        return DownloadJob.model_validate(data)
