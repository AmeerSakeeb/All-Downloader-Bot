"""Throttled progress tracking and parser for downloader execution output."""

import logging
import re
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# yt-dlp stdout progress regex patterns
_YTDLP_PERCENT_RE = re.compile(r"\[download\]\s+(\d+\.\d+)%")
_YTDLP_SIZE_RE = re.compile(r"\[download\]\s+of\s+(\d+\.\d+)(KiB|MiB|GiB|B)")
_YTDLP_SPEED_RE = re.compile(r"at\s+(\d+\.\d+)(KiB|MiB|GiB|B)/s")
_YTDLP_ETA_RE = re.compile(r"ETA\s+(\d+:\d+|\d+:\d+:\d+|\d+s)")


class ProgressTracker:
    """Parses downloader progress and executes throttled Telegram update callbacks."""

    def __init__(
        self,
        on_update_cb: Callable[[float, int, Optional[int], float, Optional[int]], None],
        throttle_secs: float = 2.0
    ):
        self.on_update_cb = on_update_cb
        self.throttle_secs = throttle_secs
        
        self.last_update_time = 0.0
        self.last_pct = -1.0

    def parse_line(self, line: str, expected_size: Optional[int] = None) -> None:
        """Parse a single output line from yt-dlp to extract download progress."""
        if not line or not line.startswith("[download]"):
            return

        pct_match = _YTDLP_PERCENT_RE.search(line)
        if not pct_match:
            return

        try:
            pct = float(pct_match.group(1))
        except ValueError:
            return

        # Read other parameters
        speed = 0.0
        speed_match = _YTDLP_SPEED_RE.search(line)
        if speed_match:
            try:
                val = float(speed_match.group(1))
                unit = speed_match.group(2)
                # Convert to bytes/sec
                mult = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(unit, 1)
                speed = val * mult
            except ValueError:
                pass

        eta = None
        eta_match = _YTDLP_ETA_RE.search(line)
        if eta_match:
            eta_str = eta_match.group(1)
            eta = self._parse_eta_str(eta_str)

        # Estimate downloaded bytes
        total_bytes = expected_size
        size_match = _YTDLP_SIZE_RE.search(line)
        if size_match:
            try:
                val = float(size_match.group(1))
                unit = size_match.group(2)
                mult = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(unit, 1)
                total_bytes = int(val * mult)
            except ValueError:
                pass

        downloaded = 0
        if total_bytes:
            downloaded = int(total_bytes * (pct / 100.0))

        # Check throttling
        now = time.time()
        # Always allow first (0%), final (100%), or large percentage jumps, or throttle time elapsed
        is_first = self.last_pct < 0
        is_final = pct >= 100.0
        time_elapsed = (now - self.last_update_time) >= self.throttle_secs
        significant_change = abs(pct - self.last_pct) >= 5.0

        if is_first or is_final or time_elapsed or significant_change:
            self.last_update_time = now
            self.last_pct = pct
            try:
                self.on_update_cb(pct, downloaded, total_bytes, speed, eta)
            except Exception as e:
                logger.debug(f"Progress update callback failed: {e}")

    def _parse_eta_str(self, eta_str: str) -> Optional[int]:
        """Convert HH:MM:SS or MM:SS or Xs into seconds."""
        if not eta_str:
            return None
        if eta_str.endswith("s"):
            try:
                return int(eta_str[:-1])
            except ValueError:
                return None
        parts = eta_str.split(":")
        try:
            if len(parts) == 2:  # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:  # HH:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            pass
        return None
