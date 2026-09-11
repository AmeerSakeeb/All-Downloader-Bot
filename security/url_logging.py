"""Central URL and credential-safe logging helpers."""

from urllib.parse import urlsplit, urlunsplit


def sanitize_url_for_log(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "<redacted>"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(
        (parsed.scheme, f"{parsed.hostname}{port}", parsed.path[:160] or "/", "", "")
    )


def sanitize_log_value(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return sanitize_url_for_log(value)
    return value
