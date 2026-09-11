"""Core module for Any Video Downloader Bot."""

from core.config import Settings, get_settings, ResourceMode, SendMode
from core.exceptions import (
    BotError,
    SecurityError,
    SSRFError,
    AccessDeniedError,
    ExtractionError,
    UnsupportedUrlError,
    MediaUnavailableError,
    DRMProtectedError,
    DownloadError,
    ResourceExhaustedError,
    InsufficientDiskSpaceError,
    StreamIncompatibleError,
    JobCancelledError,
)
from core.logging import setup_logging
from core.models import (
    VideoCodec,
    AudioCodec,
    MediaFormat,
    MediaSession,
    DownloadJob,
    JobStatus,
)

__all__ = [
    "Settings",
    "get_settings",
    "ResourceMode",
    "SendMode",
    "BotError",
    "SecurityError",
    "SSRFError",
    "AccessDeniedError",
    "ExtractionError",
    "UnsupportedUrlError",
    "MediaUnavailableError",
    "DRMProtectedError",
    "DownloadError",
    "ResourceExhaustedError",
    "InsufficientDiskSpaceError",
    "StreamIncompatibleError",
    "JobCancelledError",
    "setup_logging",
    "VideoCodec",
    "AudioCodec",
    "MediaFormat",
    "MediaSession",
    "DownloadJob",
    "JobStatus",
]
