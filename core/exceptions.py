"""Structured exception hierarchy for categorized error reporting."""

from enum import Enum
from typing import Optional


class ErrorCategory(str, Enum):
    UNEXPECTED_ERROR = "unexpected_error"
    UNSAFE_URL = "unsafe_url"
    ACCESS_DENIED = "access_denied"
    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_TIMEOUT = "extraction_timeout"
    EXTRACTOR_COMPATIBILITY_FAILURE = "extractor_compatibility_failure"
    NO_FORMATS = "no_formats"
    NETWORK_FAILURE = "network_failure"
    GEO_RESTRICTED = "geo_restricted"
    SITE_ACCESS_CHALLENGE = "site_access_challenge"
    UNSUPPORTED_URL = "unsupported_url"
    MEDIA_UNAVAILABLE = "media_unavailable"
    DRM_UNSUPPORTED = "drm_unsupported"
    DOWNLOAD_FAILED = "download_failed"
    EXACT_FORMAT_DISAPPEARED = "exact_format_disappeared"
    AUTHENTICATION_REQUIRED = "authentication_required"
    RESOURCE_WAIT = "resource_wait"
    INSUFFICIENT_DISK = "insufficient_disk"
    LOSSLESS_MERGE_UNAVAILABLE = "lossless_merge_unavailable"
    CANCELLED = "cancelled"
    DELIVERY_SIZE_EXCEEDED = "delivery_size_exceeded"
    TELEGRAM_DELIVERY_UNAVAILABLE = "telegram_delivery_unavailable"
    TELEGRAM_DELIVERY_FAILED = "telegram_delivery_failed"


class BotError(Exception):
    """Base exception for all bot errors."""
    error_category: str = ErrorCategory.UNEXPECTED_ERROR.value

    def __init__(self, message: str, user_message: Optional[str] = None):
        super().__init__(message)
        self.message = message
        # Clean message suitable to show to the Telegram user
        self.user_message = user_message or message


class SecurityError(BotError):
    """Raised when a security validation fails (SSRF, path traversal, etc.)."""
    error_category: str = ErrorCategory.UNSAFE_URL.value


class SSRFError(SecurityError):
    """Raised when an outbound URL points to a forbidden destination."""
    error_category: str = ErrorCategory.UNSAFE_URL.value

    def __init__(self, url: str, reason: str):
        super().__init__(
            f"SSRF violation for URL {url}: {reason}",
            user_message="⚠️ Access to this URL is blocked for security reasons."
        )


class AccessDeniedError(BotError):
    """Raised when an unauthorized user attempts an operation."""
    error_category: str = ErrorCategory.ACCESS_DENIED.value

    def __init__(self, user_id: int):
        super().__init__(
            f"User {user_id} is not in the allowlist",
            user_message="⛔ You are not authorized to use this bot."
        )


class ExtractionError(BotError):
    """Raised when metadata extraction fails."""
    error_category: str = ErrorCategory.EXTRACTION_FAILED.value


class ExtractionTimeoutError(ExtractionError):
    error_category: str = ErrorCategory.EXTRACTION_TIMEOUT.value

    def __init__(self):
        super().__init__(
            "All bounded media extraction attempts timed out",
            user_message="The website did not respond before the media-analysis timeout. Please try again later.",
        )


class ExtractorCompatibilityError(ExtractionError):
    error_category: str = ErrorCategory.EXTRACTOR_COMPATIBILITY_FAILURE.value

    def __init__(self):
        super().__init__(
            "The extractor could not parse otherwise reachable source metadata",
            user_message="The website changed how this media is provided. Please try again later.",
        )


class NoFormatsError(ExtractionError):
    error_category: str = ErrorCategory.NO_FORMATS.value

    def __init__(self):
        super().__init__(
            "No genuine usable source formats were returned",
            user_message="No downloadable source qualities were found.",
        )


class NetworkFailureError(ExtractionError):
    error_category: str = ErrorCategory.NETWORK_FAILURE.value

    def __init__(self):
        super().__init__(
            "The extractor transport failed",
            user_message="The source network connection failed. Please try again later.",
        )


class GeoRestrictedError(ExtractionError):
    error_category: str = ErrorCategory.GEO_RESTRICTED.value

    def __init__(self):
        super().__init__(
            "The source reported a geographic restriction",
            user_message="This media is not available from the bot's region.",
        )


class SiteAccessChallengeError(ExtractionError):
    error_category: str = ErrorCategory.SITE_ACCESS_CHALLENGE.value

    def __init__(self):
        super().__init__(
            "The source rejected the automated media request",
            user_message=(
                "This website rejected the server's automated media request. "
                "An authorized session or different supported access method may be required."
            ),
        )


class UnsupportedUrlError(ExtractionError):
    error_category: str = ErrorCategory.UNSUPPORTED_URL.value

    def __init__(self, url: str):
        super().__init__(
            f"Unsupported URL: {url}",
            user_message="❌ This website or media URL is not supported."
        )


class MediaUnavailableError(ExtractionError):
    error_category: str = ErrorCategory.MEDIA_UNAVAILABLE.value

    def __init__(self, url: str, reason: str = "Media not found"):
        super().__init__(
            f"Media unavailable at {url}: {reason}",
            user_message="❌ This media is unavailable, private, or has been deleted."
        )


class DRMProtectedError(ExtractionError):
    error_category: str = ErrorCategory.DRM_UNSUPPORTED.value

    def __init__(self, url: str):
        super().__init__(
            f"DRM protected content at {url}",
            user_message="🔒 This content is DRM-protected and cannot be downloaded."
        )


class DownloadError(BotError):
    """Raised when a download operation fails."""
    error_category: str = ErrorCategory.DOWNLOAD_FAILED.value


class ExactFormatUnavailableError(BotError):
    """The immutable source-format selection disappeared or materially changed."""

    error_category: str = ErrorCategory.EXACT_FORMAT_DISAPPEARED.value

    def __init__(self):
        super().__init__(
            "The selected exact source format is no longer available",
            user_message=(
                "The exact source format you selected is no longer available. "
                "Please analyze the link again and choose from the currently available formats."
            ),
        )


class AuthenticationRequiredError(ExtractionError):
    error_category: str = ErrorCategory.AUTHENTICATION_REQUIRED.value

    def __init__(self):
        super().__init__(
            "The source requires an authenticated session",
            user_message="An authorized session is required for this source. The configured session may be absent or expired.",
        )


class ResourceExhaustedError(BotError):
    """Raised when system resources (RAM, disk) prevent starting an operation."""
    error_category: str = ErrorCategory.RESOURCE_WAIT.value


class InsufficientDiskSpaceError(ResourceExhaustedError):
    error_category: str = ErrorCategory.INSUFFICIENT_DISK.value

    def __init__(self, required_bytes: int, available_bytes: int):
        req_mb = required_bytes / (1024 * 1024)
        avail_mb = available_bytes / (1024 * 1024)
        super().__init__(
            f"Insufficient disk space: required {req_mb:.1f}MB, available {avail_mb:.1f}MB",
            user_message="⚠️ Server storage is temporarily full. Please try again later."
        )


class StreamIncompatibleError(BotError):
    """Raised when selected streams cannot be losslessly combined without transcoding."""
    error_category: str = ErrorCategory.LOSSLESS_MERGE_UNAVAILABLE.value

    def __init__(self, video_codec: str, audio_codec: str, container: str):
        super().__init__(
            f"Cannot losslessly merge {video_codec} and {audio_codec} into {container}",
            user_message=(
                "⚠️ These streams cannot be combined losslessly without re-encoding.\n"
                "To preserve original quality, transcoding is disabled."
            )
        )


class JobCancelledError(BotError):
    """Raised when a job is cancelled by the user or admin."""
    error_category: str = ErrorCategory.CANCELLED.value

    def __init__(self, job_id: str):
        super().__init__(f"Job {job_id} was cancelled", user_message="⏹️ Download cancelled.")
