"""Central URL and credential-safe logging helpers."""

from urllib.parse import urlsplit, urlunsplit
import re


def sanitize_url_for_log(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "<redacted>"
    return urlunsplit(
        (parsed.scheme, f"{parsed.hostname}{port}", parsed.path[:160] or "/", "", "")
    )


def sanitize_log_value(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return sanitize_url_for_log(value)
    return value


def sanitize_diagnostic(value: str) -> str:
    """Remove all URL queries/credentials and sensitive header lines, including tracebacks."""
    value = re.sub(r"(?im)^.*(?:authorization|cookie|set-cookie)\s*[:=].*$", "<sensitive line redacted>", value)
    value = re.sub(r"https?://[^\s<>\"']+", lambda m: sanitize_url_for_log(m.group(0)), value)
    value = re.sub(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b", "<token-redacted>", value)
    return value
