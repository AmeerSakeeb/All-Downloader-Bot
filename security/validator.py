"""URL validation, canonicalization, and path sanitization."""

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Parameters commonly used for tracking that are safe to strip without altering media content
_STRIPPABLE_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "igshid",
    "_hsenc",
    "_hsmi",
    "mc_cid",
    "mc_eid",
}


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """Sanitize a filename to prevent path traversal and invalid characters."""
    if not name:
        return "unnamed_media"

    # Replace forbidden path characters
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    # Remove control characters and null bytes
    clean = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", clean)
    # Strip leading/trailing dots and spaces
    clean = clean.strip(". ")

    if not clean:
        clean = "unnamed_media"

    # Truncate while preserving extension if possible
    if len(clean) > max_length:
        path = Path(clean)
        ext = path.suffix
        stem = path.stem[: max_length - len(ext) - 1]
        clean = f"{stem}{ext}"

    return clean


def canonicalize_url(url: str) -> str:
    """Safely strip known tracking parameters while preserving media/auth query params."""
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    if not parsed.query:
        return url

    # Parse query parameters
    query_params = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_params = [
        (k, v) for k, v in query_params if k.lower() not in _STRIPPABLE_TRACKING_PARAMS
    ]

    new_query = urlencode(filtered_params)
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        ""  # Strip fragment
    ))
