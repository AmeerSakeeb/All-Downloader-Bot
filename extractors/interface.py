"""Abstract base class and registry for media extractors."""

from typing import Protocol, runtime_checkable
from core.models import MediaSession


@runtime_checkable
class Extractor(Protocol):
    """Protocol defining interface for metadata extractors."""

    async def can_extract(self, url: str) -> bool:
        """Return True if this extractor can handle the given URL."""
        ...

    async def extract(
        self, url: str, user_id: int, *, operation_id: str | None = None,
        prefer_impersonation: bool = False, cookie_profile: str | None = None,
    ) -> MediaSession:
        """
        Extract metadata and available formats for the given URL.
        operation_id identifies the process owner for cancellable extraction.
        prefer_impersonation reuses a transport profile selected during analysis.
        Raises ExtractionError or its subclasses on failure.
        """
        ...
