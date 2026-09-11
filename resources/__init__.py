"""Resource adaptivity, detection, and governor."""

from resources.detector import ResourceDetector
from resources.governor import ResourceGovernor

__all__ = ["ResourceDetector", "ResourceGovernor"]
