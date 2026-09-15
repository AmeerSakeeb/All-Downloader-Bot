import asyncio
import sqlite3
import time

import pytest

from core.models import JobStatus, MediaSession
from storage.database import CURRENT_SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_migrations_reach_current(db):
    assert await db.schema_version() == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_from_v1_fixture(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
            INSERT INTO schema_migrations VALUES(1, 1);
            CREATE TABLE download_jobs(
              job_id TEXT PRIMARY KEY, media_session_id TEXT NOT NULL, user_id INTEGER NOT NULL,
              chat_id INTEGER NOT NULL, message_id INTEGER, video_format_id TEXT NOT NULL,
              audio_format_id TEXT, status TEXT NOT NULL, progress_pct REAL NOT NULL DEFAULT 0,
              downloaded_bytes INTEGER NOT NULL DEFAULT 0,total_bytes INTEGER,speed_bytes_sec REAL NOT NULL DEFAULT 0,
              eta_seconds INTEGER,current_stage TEXT NOT NULL,process_pid INTEGER,error_message TEXT,error_category TEXT,
              send_mode TEXT NOT NULL DEFAULT 'document',output_path TEXT,telegram_file_id TEXT,snapshot_json TEXT,
              created_at REAL NOT NULL,updated_at REAL NOT NULL);
            """
        )
    database = Database(path)
    await database.connect()
    columns = await (await database.connection.execute("PRAGMA table_info(download_jobs)")).fetchall()
    assert {row["name"] for row in columns} >= {"source_url", "claimed_at"}
    await database.close()


@pytest.mark.asyncio
async def test_user_crud(db):
    await db.add_or_update_user(2, is_allowed=True)
    assert (await db.get_user(2))["is_allowed"] is True
    assert {u["user_id"] for u in await db.list_users()} == {1, 2}
    await db.add_or_update_user(2, is_allowed=False)
    assert (await db.get_user(2))["is_allowed"] is False


@pytest.mark.asyncio
async def test_admin_bootstrap(db):
    assert (await db.get_user(1))["is_admin"] is True


@pytest.mark.asyncio
async def test_session_round_trip(db, media_session):
    await db.save_media_session(media_session)
    loaded = await db.get_media_session(media_session.session_id)
    assert loaded and loaded.formats[0].format_id == "137/unsafe-id"


@pytest.mark.asyncio
async def test_fractional_session_duration_round_trip_uses_real_affinity(db):
    session = MediaSession(
        user_id=1, url="https://example.com/facebook", duration=33.505,
    )
    await db.save_media_session(session)
    loaded = await db.get_media_session(session.session_id)
    columns = await (await db.connection.execute("PRAGMA table_info(media_sessions)")).fetchall()
    duration_column = next(row for row in columns if row["name"] == "duration")
    assert loaded and loaded.duration == 33.505
    assert duration_column["type"].upper() == "REAL"


@pytest.mark.asyncio
async def test_duration_migration_preserves_existing_session_rows(tmp_path):
    path = tmp_path / "duration-v10.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
            INSERT INTO schema_migrations VALUES(10, 1);
            CREATE TABLE media_sessions(
              session_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,url TEXT NOT NULL,
              canonical_url TEXT,extractor TEXT NOT NULL,title TEXT,duration INTEGER,
              uploader TEXT,thumbnail_url TEXT,formats_json TEXT NOT NULL,
              created_at REAL NOT NULL,expires_at REAL NOT NULL,
              extras_json TEXT NOT NULL DEFAULT '{}');
            INSERT INTO media_sessions VALUES(
              'legacy',1,'https://example.com/v',NULL,'test','Legacy',33.505,
              NULL,NULL,'[]',1,99999999999,'{}');
            """
        )
    database = Database(path)
    await database.connect()
    loaded = await database.get_media_session("legacy")
    columns = await (await database.connection.execute("PRAGMA table_info(media_sessions)")).fetchall()
    assert loaded and loaded.duration == 33.505
    assert next(row for row in columns if row["name"] == "duration")["type"] == "REAL"
    await database.close()


def test_session_ttl_uses_config(media_session, settings):
    assert media_session.expires_at - media_session.created_at == settings.media_session_ttl


@pytest.mark.asyncio
async def test_job_creation_snapshots_execution(db, media_session, video_format, audio_format):
    media_session.thumbnail_url = "https://cdn.example.com/cover.jpg?signature=secret"
    media_session.duration = 33.505
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=99
    )
    loaded = await db.get_job(job.job_id)
    assert loaded.video_format_snapshot["format_id"] == "137/unsafe-id"
    assert loaded.audio_format_snapshot["format_id"] == "140"
    assert loaded.source_url == media_session.url
    assert loaded.thumbnail_url == media_session.thumbnail_url
    assert loaded.source_duration == 33.505


@pytest.mark.asyncio
async def test_job_creation_rejects_expired(db, media_session, video_format, audio_format):
    expired = media_session.model_copy(update={"expires_at": time.time() - 1})
    await db.save_media_session(expired)
    with pytest.raises(ValueError):
        await db.create_download_job(
            session=expired, video_format=video_format, audio_format=audio_format, chat_id=99
        )


@pytest.mark.asyncio
async def test_claim_is_atomic(db, media_session, video_format, audio_format):
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=99
    )
    assert await db.claim_job(job.job_id)
    assert not await db.claim_job(job.job_id)


@pytest.mark.asyncio
async def test_expired_session_cleanup_keeps_job_snapshot(db, media_session, video_format, audio_format):
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=99
    )
    await db.connection.execute(
        "UPDATE media_sessions SET expires_at=? WHERE session_id=?", (0, media_session.session_id)
    )
    assert await db.delete_expired_sessions() == 1
    assert (await db.get_job(job.job_id)).video_format_snapshot


@pytest.mark.asyncio
async def test_atomic_disk_reservation(db, media_session, video_format, audio_format):
    await db.save_media_session(media_session)
    first = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=1
    )
    second = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=1
    )
    results = await asyncio.gather(
        db.reserve_disk_atomic(first.job_id, 600, 1000, 100),
        db.reserve_disk_atomic(second.job_id, 600, 1000, 100),
    )
    assert sorted(results) == [False, True]
    assert await db.get_total_reserved_bytes() == 600


@pytest.mark.asyncio
async def test_disk_reservation_growth_extends_when_headroom_is_safe(
    db, media_session, video_format
):
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=None, chat_id=1
    )
    assert await db.reserve_disk_atomic(job.job_id, 100, 1_000, 100)

    assert await db.extend_disk_reservation_atomic(
        job.job_id, 90, 900, 100, 100
    )

    row = await (
        await db.connection.execute(
            "SELECT projected_bytes,actual_bytes,reserved_bytes "
            "FROM disk_reservations WHERE job_id=?",
            (job.job_id,),
        )
    ).fetchone()
    assert tuple(row) == (190, 90, 100)


@pytest.mark.asyncio
async def test_disk_reservation_growth_refuses_to_cross_headroom(
    db, media_session, video_format
):
    await db.save_media_session(media_session)
    first = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=None, chat_id=1
    )
    second = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=None, chat_id=2
    )
    assert await db.reserve_disk_atomic(first.job_id, 100, 1_000, 100)
    assert await db.reserve_disk_atomic(second.job_id, 700, 1_000, 100)

    assert not await db.extend_disk_reservation_atomic(
        first.job_id, 100, 800, 100, 100
    )


@pytest.mark.asyncio
async def test_list_queued_and_recovery_queries(db, media_session, video_format, audio_format):
    await db.save_media_session(media_session)
    job = await db.create_download_job(
        session=media_session, video_format=video_format, audio_format=audio_format, chat_id=1
    )
    assert [j.job_id for j in await db.list_queued_jobs()] == [job.job_id]
    job.status = JobStatus.WAITING_RESOURCES
    await db.save_job(job)
    assert (await db.get_active_jobs())[0].status == JobStatus.WAITING_RESOURCES
