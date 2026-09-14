import pytest

from core.config import ResourceMode
from resources.detector import ResourceDetector
from resources.governor import ResourceGovernor
from storage.file_manager import FileManager


class Detector:
    sample_value = {
        "memory": {"total": 8 * 1024**3, "available": 6 * 1024**3, "used": 2 * 1024**3},
        "cpu": {"cores": 8.0, "load_percent": 10.0, "logical_cores": 8},
        "load_average": (0.2, 0.2, 0.2),
        "process": {"process_rss": 100 * 1024**2, "child_rss": 0, "child_count": 0},
    }

    @classmethod
    async def sample(cls):
        return cls.sample_value


@pytest.mark.asyncio
async def test_adaptive_shared_preserves_more_headroom(db, settings):
    manager = FileManager(settings.jobs_dir)
    settings.max_concurrent_downloads = 0
    settings.resource_mode = ResourceMode.AUTO_SHARED
    shared = ResourceGovernor(db, manager, settings, Detector)
    shared_limit = (await shared.adaptive_limits())["download"]
    settings.resource_mode = ResourceMode.AUTO_DEDICATED
    dedicated = ResourceGovernor(db, manager, settings, Detector)
    assert (await dedicated.adaptive_limits())["download"] > shared_limit


@pytest.mark.asyncio
async def test_external_child_pressure_reduces_capacity(db, settings):
    manager = FileManager(settings.jobs_dir)
    settings.resource_mode = ResourceMode.AUTO_SHARED
    Detector.sample_value = {
        **Detector.sample_value,
        "process": {"process_rss": 100, "child_rss": 100, "child_count": 0},
    }
    low = ResourceGovernor(db, manager, settings, Detector)
    baseline = (await low.adaptive_limits())["download"]
    Detector.sample_value = {
        **Detector.sample_value,
        "process": {"process_rss": 100, "child_rss": 3 * 1024**3, "child_count": 8},
    }
    pressured = ResourceGovernor(db, manager, settings, Detector)
    assert (await pressured.adaptive_limits())["download"] < baseline


@pytest.mark.asyncio
async def test_stage_lease_releases_after_exception(db, settings):
    governor = ResourceGovernor(db, FileManager(settings.jobs_dir), settings, Detector)
    lease, _ = await governor.acquire_stage("merge")
    with pytest.raises(RuntimeError):
        async with lease:
            raise RuntimeError("boom")
    assert governor.active_counts["merge"] == 0


@pytest.mark.asyncio
async def test_stage_admission_denial(db, settings):
    governor = ResourceGovernor(db, FileManager(settings.jobs_dir), settings, Detector)
    first, _ = await governor.acquire_stage("merge")
    second, _ = await governor.acquire_stage("merge")
    assert first is not None and second is None
    await first.release()


@pytest.mark.asyncio
async def test_configured_download_target_two_allows_two_when_healthy(db, settings):
    settings.max_concurrent_downloads = 2
    governor = ResourceGovernor(db, FileManager(settings.jobs_dir), settings, Detector)
    first, _ = await governor.acquire_stage("download")
    second, _ = await governor.acquire_stage("download")
    third, _ = await governor.acquire_stage("download")
    assert first is not None and second is not None and third is None
    await first.release()
    await second.release()


def test_cgroup_memory_limit(monkeypatch):
    class Memory:
        total = 16_000
        available = 10_000
        used = 6_000

    monkeypatch.setattr("resources.detector.psutil.virtual_memory", lambda: Memory())
    monkeypatch.setattr(
        "resources.detector.read_cgroup_file",
        lambda path: "8000" if str(path).endswith("memory.max") else "3000",
    )
    assert ResourceDetector.get_memory_info() == {"total": 8000, "available": 5000, "used": 3000}


def test_cgroup_cpu_quota(monkeypatch):
    monkeypatch.setattr("resources.detector.psutil.cpu_count", lambda logical: 8)
    monkeypatch.setattr("resources.detector.psutil.cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr("resources.detector.read_cgroup_file", lambda path: "150000 100000")
    assert ResourceDetector.get_cpu_info()["cores"] == 1.5


@pytest.mark.asyncio
async def test_memory_pressure_denies_admission(db, settings):
    Detector.sample_value = {
        **Detector.sample_value,
        "memory": {"total": 1000, "available": 1, "used": 999},
    }
    settings.resource_mode = ResourceMode.AUTO_SHARED
    governor = ResourceGovernor(db, FileManager(settings.jobs_dir), settings, Detector)
    lease, reason = await governor.acquire_stage("download")
    assert lease is None and "memory" in reason
