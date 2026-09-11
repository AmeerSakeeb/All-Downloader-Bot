"""Structured exception hierarchy for categorized error reporting."""

from typing import Optional


class BotError(Exception):
    """Base exception for all bot errors."""
    error_category: str = "unexpected_error"

    def __init__(self, message: str, user_message: Optional[str] = None):
        super().__init__(message)
        self.message = message
        # Clean message suitable to show to the Telegram user
        self.user_message = user_message or message


class SecurityError(BotError):
    """Raised when a security validation fails (SSRF, path traversal, etc.)."""
    error_category: str = "unsafe_url"


class SSRFError(SecurityError):
    """Raised when an outbound URL points to a forbidden destination."""
    error_category: str = "unsafe_url"

    def __init__(self, url: str, reason: str):
        super().__init__(
            f"SSRF violation for URL {url}: {reason}",
            user_message="⚠️ Access to this URL is blocked for security reasons."
        )


class AccessDeniedError(BotError):
    """Raised when an unauthorized user attempts an operation."""
    error_category: str = "access_denied"

    def __init__(self, user_id: int):
        super().__init__(
            f"User {user_id} is not in the allowlist",
            user_message="⛔ You are not authorized to use this bot."
        )


class ExtractionError(BotError):
    """Raised when metadata extraction fails."""
    error_category: str = "extraction_failed"


class ExtractionTimeoutError(ExtractionError):
    error_category: str = "extraction_timeout"

    def __init__(self):
        super().__init__(
            "All bounded media extraction attempts timed out",
            user_message="The website did not respond before the media-analysis timeout. Please try again later.",
        )


class SiteAccessChallengeError(ExtractionError):
    error_category: str = "site_access_challenge"

    def __init__(self):
        super().__init__(
            "The source rejected the automated media request",
            user_message=(
                "This website rejected the server's automated media request. "
                "An authorized session or different supported access method may be required."
            ),
        )


class UnsupportedUrlError(ExtractionError):
    error_category: str = "unsupported_url"

    def __init__(self, url: str):
        super().__init__(
            f"Unsupported URL: {url}",
            user_message="❌ This website or media URL is not supported."
        )


class MediaUnavailableError(ExtractionError):
    error_category: str = "media_unavailable"

    def __init__(self, url: str, reason: str = "Media not found"):
        super().__init__(
            f"Media unavailable at {url}: {reason}",
            user_message="❌ This media is unavailable, private, or has been deleted."
        )


class DRMProtectedError(ExtractionError):
    error_category: str = "drm_unsupported"

    def __init__(self, url: str):
        super().__init__(
            f"DRM protected content at {url}",
            user_message="🔒 This content is DRM-protected and cannot be downloaded."
        )


class DownloadError(BotError):
    """Raised when a download operation fails."""
    error_category: str = "download_failed"


class ExactFormatUnavailableError(BotError):
    """The immutable source-format selection disappeared or materially changed."""

    error_category: str = "exact_format_disappeared"

    def __init__(self):
        super().__init__(
            "The selected exact source format is no longer available",
            user_message=(
                "The exact source format you selected is no longer available. "
                "Please analyze the link again and choose from the currently available formats."
            ),
        )


class AuthenticationRequiredError(ExtractionError):
    error_category: str = "authentication_required"

    def __init__(self):
        super().__init__(
            "The source requires an authenticated session",
            user_message="This media requires an authorized session that is not configured.",
        )


class ResourceExhaustedError(BotError):
    """Raised when system resources (RAM, disk) prevent starting an operation."""
    error_category: str = "resource_wait"


class InsufficientDiskSpaceError(ResourceExhaustedError):
    error_category: str = "insufficient_disk"

    def __init__(self, required_bytes: int, available_bytes: int):
        req_mb = required_bytes / (1024 * 1024)
        avail_mb = available_bytes / (1024 * 1024)
        super().__init__(
            f"Insufficient disk space: required {req_mb:.1f}MB, available {avail_mb:.1f}MB",
            user_message="⚠️ Server storage is temporarily full. Please try again later."
        )


class StreamIncompatibleError(BotError):
    """Raised when selected streams cannot be losslessly combined without transcoding."""
    error_category: str = "lossless_merge_unavailable"

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
    error_category: str = "cancelled"

    def __init__(self, job_id: str):
        super().__init__(f"Job {job_id} was cancelled", user_message="⏹️ Download cancelled.")
