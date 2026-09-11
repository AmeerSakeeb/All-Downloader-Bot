"""Safe persisted messages for domain failures."""

from core.exceptions import BotError

SAFE_USER_CATEGORIES = {
    "authentication_required", "drm_unsupported", "media_unavailable",
    "unsupported_url", "insufficient_disk", "lossless_merge_unavailable",
    "exact_format_disappeared", "delivery_size_exceeded", "access_denied",
}


def safe_job_error(error: Exception) -> str:
    if isinstance(error, BotError) and error.error_category in SAFE_USER_CATEGORIES:
        return error.user_message
    return "The download could not be completed. Please retry or contact the administrator."
