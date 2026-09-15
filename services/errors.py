"""Safe persisted messages for domain failures."""

from core.exceptions import BotError, ErrorCategory

SAFE_USER_CATEGORIES = {
    ErrorCategory.AUTHENTICATION_REQUIRED.value,
    ErrorCategory.DRM_UNSUPPORTED.value,
    ErrorCategory.MEDIA_UNAVAILABLE.value,
    ErrorCategory.UNSUPPORTED_URL.value,
    ErrorCategory.INSUFFICIENT_DISK.value,
    ErrorCategory.LOSSLESS_MERGE_UNAVAILABLE.value,
    ErrorCategory.EXACT_FORMAT_DISAPPEARED.value,
    ErrorCategory.DELIVERY_SIZE_EXCEEDED.value,
    ErrorCategory.TELEGRAM_DELIVERY_UNAVAILABLE.value,
    ErrorCategory.ACCESS_DENIED.value,
    ErrorCategory.TELEGRAM_DELIVERY_FAILED.value,
    ErrorCategory.EXTRACTION_TIMEOUT.value,
    ErrorCategory.SITE_ACCESS_CHALLENGE.value,
    ErrorCategory.EXTRACTOR_COMPATIBILITY_FAILURE.value,
    ErrorCategory.NO_FORMATS.value,
    ErrorCategory.NETWORK_FAILURE.value,
    ErrorCategory.GEO_RESTRICTED.value,
}


def safe_job_error(error: Exception) -> str:
    if isinstance(error, BotError) and error.error_category in SAFE_USER_CATEGORIES:
        return error.user_message
    return "The download could not be completed. Please retry or contact the administrator."
