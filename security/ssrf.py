"""SSRF protection and IP validation for all outbound network operations."""

import ipaddress
import logging
import socket
from typing import List, Tuple
from urllib.parse import urlparse

from core.exceptions import SSRFError
from security.url_logging import sanitize_url_for_log

logger = logging.getLogger(__name__)

# Cloud metadata IP addresses to explicitly block
_BLOCKED_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/DigitalOcean metadata
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
    ipaddress.ip_address("fd00:ec2::254"),    # AWS IPv6 metadata
}

# Blocked hostnames
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "instance-data",
}


def is_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Tuple[bool, str]:
    """Check if an IP address is safe for outbound access."""
    if ip in _BLOCKED_METADATA_IPS:
        return False, f"Cloud metadata IP address {ip} is blocked"

    if ip.is_loopback:
        return False, f"Loopback address {ip} is blocked"

    if ip.is_private:
        return False, f"Private network address {ip} is blocked"

    if ip.is_link_local:
        return False, f"Link-local address {ip} is blocked"

    if ip.is_multicast:
        return False, f"Multicast address {ip} is blocked"

    if ip.is_reserved:
        return False, f"Reserved address {ip} is blocked"

    if ip.is_unspecified:
        return False, f"Unspecified address {ip} is blocked"

    if not ip.is_global:
        return False, f"Non-public address {ip} is blocked"

    # Specific IPv6 link-local and documentation ranges
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_site_local:
            return False, f"IPv6 site-local address {ip} is blocked"
        # IPv4-mapped IPv6 addresses: check underlying IPv4
        if ip.ipv4_mapped:
            return is_ip_allowed(ip.ipv4_mapped)

    return True, "Allowed"


def resolve_hostname(hostname: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to all associated IP addresses."""
    try:
        # Check if the hostname is already an IP address
        ip = ipaddress.ip_address(hostname.strip("[]"))
        return [ip]
    except ValueError:
        pass

    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips = []
        for info in addr_info:
            sockaddr = info[4]
            ip_str = sockaddr[0]
            ips.append(ipaddress.ip_address(ip_str))
        return list(set(ips))  # Deduplicate
    except socket.gaierror as e:
        logger.warning(f"Failed to resolve hostname '{hostname}': {e}")
        return []


class SSRFGuard:
    """Validator for outbound URLs to prevent SSRF attacks."""

    @staticmethod
    def validate_url(url: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Validate a URL against SSRF rules. Raises SSRFError if unsafe."""
        if not url:
            raise SSRFError(url, "Empty URL provided")

        try:
            parsed = urlparse(url)
        except Exception as e:
            raise SSRFError(url, f"Malformed URL: {e}")

        # Scheme check: only http and https allowed
        if parsed.scheme.lower() not in ("http", "https"):
            raise SSRFError(url, f"Forbidden scheme '{parsed.scheme}'. Only HTTP and HTTPS are allowed.")

        hostname = parsed.hostname
        if not hostname:
            raise SSRFError(url, "URL contains no hostname")

        hostname_lower = hostname.lower()

        # Hostname blacklist check
        if hostname_lower in _BLOCKED_HOSTNAMES or hostname_lower.endswith(".internal") or hostname_lower.endswith(".local"):
            raise SSRFError(url, f"Forbidden hostname '{hostname}'")

        # Resolve hostname to IPs and validate each resolved address
        resolved_ips = resolve_hostname(hostname)
        if not resolved_ips:
            # If we cannot resolve, reject for safety
            raise SSRFError(url, f"Hostname '{hostname}' could not be resolved to any valid IP address")

        for ip in resolved_ips:
            allowed, reason = is_ip_allowed(ip)
            if not allowed:
                logger.warning(
                    "SSRF violation for %s resolved to blocked IP %s (%s)",
                    sanitize_url_for_log(url), ip, reason,
                )
                raise SSRFError(url, f"Hostname resolves to unsafe IP {ip}: {reason}")

        logger.debug(
            "SSRF validation passed for %s", sanitize_url_for_log(url)
        )
        return resolved_ips
