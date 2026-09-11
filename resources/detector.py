"""De-coupled system resource detector with cgroup v2 and host fallbacks."""

import logging
import os
import asyncio
from pathlib import Path
from typing import Dict, Optional, Tuple
import psutil

logger = logging.getLogger(__name__)

# System paths for cgroup v2
_CGROUP_V2_MEM_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V2_MEM_CURR = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def read_cgroup_file(path: Path) -> Optional[str]:
    """Read contents of a cgroup file if it exists."""
    if path.exists() and os.access(path, os.R_OK):
        try:
            return path.read_text().strip()
        except Exception as e:
            logger.debug(f"Failed to read cgroup path '{path}': {e}")
    return None


class ResourceDetector:
    """Detects available RAM, CPU quota, and load averages from host or cgroup v2."""

    @staticmethod
    def get_memory_info() -> Dict[str, int]:
        """
        Get memory metrics in bytes.
        Considers cgroup v2 limits if containerized, falling back to physical host RAM.
        Returns keys: total, available, used
        """
        # Read host memory first
        virtual_mem = psutil.virtual_memory()
        host_total = virtual_mem.total
        host_avail = virtual_mem.available

        # Check cgroup v2 limit
        mem_max_raw = read_cgroup_file(_CGROUP_V2_MEM_MAX)
        mem_curr_raw = read_cgroup_file(_CGROUP_V2_MEM_CURR)

        if mem_max_raw and mem_max_raw != "max":
            try:
                cgroup_limit = int(mem_max_raw)
                cgroup_current = int(mem_curr_raw) if mem_curr_raw else 0
                
                # If cgroup limit is less than host physical RAM, container limits apply
                if cgroup_limit < host_total:
                    cgroup_avail = max(0, cgroup_limit - cgroup_current)
                    # Cannot exceed what physical host actually has free
                    effective_avail = min(host_avail, cgroup_avail)
                    return {
                        "total": cgroup_limit,
                        "available": effective_avail,
                        "used": cgroup_current
                    }
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed parsing cgroup memory limit: {e}")

        # Host physical RAM fallback
        return {
            "total": host_total,
            "available": host_avail,
            "used": virtual_mem.used
        }

    @staticmethod
    def get_cpu_info() -> Dict[str, float]:
        """
        Get CPU configuration and current usage.
        Returns keys: cores (float count), load_percent (0-100), logical_cores (int count)
        """
        logical_cores = psutil.cpu_count(logical=True) or 1
        # interval=None is non-blocking and reports load since the previous sample.
        current_load = psutil.cpu_percent(interval=None)

        # Check cgroup v2 CPU quota
        cpu_max_raw = read_cgroup_file(_CGROUP_V2_CPU_MAX)
        if cpu_max_raw:
            try:
                # Format: "quota period" or "max period"
                parts = cpu_max_raw.split()
                if len(parts) == 2 and parts[0] != "max":
                    quota = int(parts[0])
                    period = int(parts[1])
                    cgroup_cores = quota / period
                    # If quota limits us, adjust logical core capacity
                    return {
                        "cores": min(float(logical_cores), cgroup_cores),
                        "load_percent": current_load,
                        "logical_cores": logical_cores
                    }
            except (ValueError, TypeError, ZeroDivisionError) as e:
                logger.debug(f"Failed parsing cgroup CPU quota: {e}")

        return {
            "cores": float(logical_cores),
            "load_percent": current_load,
            "logical_cores": logical_cores
        }

    @staticmethod
    def get_load_average() -> Tuple[float, float, float]:
        """Get host 1, 5, 15 min load averages."""
        if hasattr(os, "getloadavg"):
            try:
                return os.getloadavg()
            except Exception:
                pass
        return (0.0, 0.0, 0.0)

    @staticmethod
    def get_process_pressure() -> Dict[str, int]:
        process = psutil.Process()
        children = process.children(recursive=True)
        child_rss = 0
        running_children = 0
        for child in children:
            try:
                child_rss += child.memory_info().rss
                if child.is_running():
                    running_children += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {
            "process_rss": process.memory_info().rss,
            "child_rss": child_rss,
            "child_count": running_children,
        }

    @classmethod
    async def sample(cls) -> Dict[str, object]:
        """Collect a complete sample off the event loop."""
        return await asyncio.to_thread(
            lambda: {
                "memory": cls.get_memory_info(),
                "cpu": cls.get_cpu_info(),
                "load_average": cls.get_load_average(),
                "process": cls.get_process_pressure(),
            }
        )
