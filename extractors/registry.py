"""Application-owned ExtractorRegistry service using shared supervisor and proxy."""

from __future__ import annotations

import logging
from typing import Optional

from core.config import Settings, get_settings
from downloads.process_supervisor import ProcessSupervisor
from extractors.direct_extractor import DirectMediaExtractor
from extractors.interface import Extractor
from extractors.ytdlp_extractor import YtDlpExtractor
from security.proxy import ControlledOutboundProxy
from services.cookie_profiles import CookieProfiles

logger = logging.getLogger(__name__)


class ExtractorRegistry:
    """Manages extractor instances bound to application supervisor and outbound proxy."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        supervisor: Optional[ProcessSupervisor] = None,
        proxy: Optional[ControlledOutboundProxy] = None,
        profiles: Optional[CookieProfiles] = None,
    ):
        self.settings = settings or get_settings()
        self.supervisor = supervisor or ProcessSupervisor()
        self.proxy = proxy
        self.profiles = profiles

    async def get_extractor_for_url(self, url: str) -> Extractor:
        """Return the appropriate extractor instance bound to application services."""
        proxy_url = self.proxy.url if self.proxy else None

        direct = DirectMediaExtractor(self.settings, proxy_url=proxy_url)
        try:
            if await direct.can_extract(url):
                return direct
        except Exception as e:
            logger.debug("DirectMediaExtractor can_extract check failed: %s", e)

        return YtDlpExtractor(
            settings=self.settings,
            supervisor=self.supervisor,
            proxy_url=proxy_url,
            profiles=self.profiles,
        )
