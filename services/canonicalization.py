"""Conservative URL canonicalization for cache and duplicate identity."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid",
}


def canonicalize_media_url(url: str) -> str:
    """Remove fragments and known tracking keys while preserving identity/auth queries."""
    parsed = urlsplit(url.strip())
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port:
        default = (parsed.scheme.lower() == "https" and parsed.port == 443) or (
            parsed.scheme.lower() == "http" and parsed.port == 80
        )
        if not default:
            netloc = f"{host}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(safe_query), "")
    )
