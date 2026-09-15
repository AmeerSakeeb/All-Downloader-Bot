"""Exact-domain extraction policy overrides layered over generic yt-dlp support."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


class SiteBehavior(str, Enum):
    NORMAL = "normal"
    IMPERSONATION_FIRST = "impersonation_first"
    CHALLENGE_PRONE = "challenge_prone"
    AUTH_OPTIONAL = "auth_optional"
    COMPATIBILITY_OVERRIDE = "compatibility_override"


@dataclass(frozen=True)
class SitePolicy:
    """A bounded behavioral override for verified official hostnames."""

    policy_id: str
    canonical_name: str
    exact_domains: frozenset[str] = field(default_factory=frozenset)
    behavior: SiteBehavior = SiteBehavior.NORMAL
    initial_impersonation: bool = False
    retry_impersonation_on_challenge: bool = True
    profile_allowed: bool = True
    compatibility_override: str | None = None
    expected_failure_mapping: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    notes: str = ""


GENERIC_SITE_POLICY = SitePolicy(
    policy_id="generic",
    canonical_name="Website",
    behavior=SiteBehavior.AUTH_OPTIONAL,
    notes="Secure generic yt-dlp extraction remains the default.",
)


class SitePolicyRegistry:
    """Resolve exact official domains without becoming a source allowlist."""

    def __init__(self, policies: tuple[SitePolicy, ...] | None = None):
        self.policies = policies or (
            SitePolicy(
                policy_id="tiktok",
                canonical_name="TikTok",
                exact_domains=frozenset({
                    "tiktok.com", "www.tiktok.com", "vt.tiktok.com", "vm.tiktok.com",
                }),
                behavior=SiteBehavior.IMPERSONATION_FIRST,
                initial_impersonation=True,
                retry_impersonation_on_challenge=False,
                profile_allowed=True,
                notes="Official TikTok and exact short-link hosts only.",
            ),
        )
        by_domain: dict[str, SitePolicy] = {}
        ids: set[str] = set()
        for policy in self.policies:
            if policy.policy_id in ids:
                raise ValueError(f"Duplicate site policy ID: {policy.policy_id}")
            ids.add(policy.policy_id)
            for domain in policy.exact_domains:
                normalized = domain.lower().rstrip(".")
                if normalized in by_domain:
                    raise ValueError(f"Duplicate exact-domain policy: {normalized}")
                by_domain[normalized] = policy
        self._by_domain = MappingProxyType(by_domain)

    def resolve(self, url: str) -> SitePolicy:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return self._by_domain.get(host, GENERIC_SITE_POLICY)

    def canonical_name(self, url: str) -> str:
        policy = self.resolve(url)
        if policy is not GENERIC_SITE_POLICY:
            return policy.canonical_name
        host = (urlsplit(url).hostname or "Website").lower().rstrip(".")
        return host[4:] if host.startswith("www.") else host
