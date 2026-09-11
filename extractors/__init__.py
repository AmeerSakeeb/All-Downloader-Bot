"""Extraction registry and normalizers."""

from extractors.codec_normalizer import normalize_audio_codec, normalize_video_codec
from extractors.direct_extractor import DirectMediaExtractor
from extractors.format_manager import (
    calculate_total_download_size,
    normalize_format_inventory,
    select_default_audio,
)
from extractors.interface import Extractor
from extractors.registry import ExtractorRegistry
from extractors.ytdlp_extractor import YtDlpExtractor

__all__ = [
    "Extractor",
    "ExtractorRegistry",
    "YtDlpExtractor",
    "DirectMediaExtractor",
    "normalize_video_codec",
    "normalize_audio_codec",
    "normalize_format_inventory",
    "select_default_audio",
    "calculate_total_download_size",
]

