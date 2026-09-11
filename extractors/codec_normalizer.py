"""Codec normalization for H.264, H.265/HEVC, VP9, AV1, and Audio codecs."""

from typing import Optional, Tuple
from core.models import AudioCodec, VideoCodec


def normalize_video_codec(raw_vcodec: Optional[str]) -> Tuple[VideoCodec, Optional[str]]:
    """
    Normalize raw video codec string to VideoCodec enum while preserving the raw string.
    
    Examples:
    - "h264", "avc", "avc1.640028" -> VideoCodec.H264
    - "h265", "hevc", "hev1.1.6.L93.B0", "hvc1.1.6.L93.B0" -> VideoCodec.H265
    - "vp9", "vp09.00.51.08" -> VideoCodec.VP9
    - "av1", "av01.0.08M.08" -> VideoCodec.AV1
    - "none" -> VideoCodec.NONE
    """
    if not raw_vcodec:
        return VideoCodec.NONE, None

    codec = raw_vcodec.lower().strip()

    if codec in ("none", ""):
        return VideoCodec.NONE, raw_vcodec

    # H.264 / AVC
    # Note: "mp4v" is MPEG-4 Part 2 codec, NOT H.264/AVC. We avoid matching mp4v here
    # to prevent false H.264 classification. Raw codec string is preserved for later inspection.
    if any(codec.startswith(prefix) for prefix in ("avc", "h264", "h.264")):
        return VideoCodec.H264, raw_vcodec

    # H.265 / HEVC
    if any(codec.startswith(prefix) for prefix in ("hevc", "h265", "h.265", "hev1", "hvc1")):
        return VideoCodec.H265, raw_vcodec

    # VP9
    if any(codec.startswith(prefix) for prefix in ("vp9", "vp09")):
        return VideoCodec.VP9, raw_vcodec

    # AV1
    if any(codec.startswith(prefix) for prefix in ("av1", "av01")):
        return VideoCodec.AV1, raw_vcodec

    # VP8
    if codec.startswith("vp8") or codec.startswith("vp08"):
        return VideoCodec.OTHER, raw_vcodec

    return VideoCodec.OTHER, raw_vcodec


def normalize_audio_codec(raw_acodec: Optional[str]) -> Tuple[AudioCodec, Optional[str]]:
    """
    Normalize raw audio codec string to AudioCodec enum while preserving raw string.
    
    Examples:
    - "aac", "mp4a.40.2" -> AudioCodec.AAC
    - "opus" -> AudioCodec.OPUS
    - "mp3", "mp4a.40.34" -> AudioCodec.MP3
    - "vorbis" -> AudioCodec.VORBIS
    - "flac" -> AudioCodec.FLAC
    - "none" -> AudioCodec.NONE
    """
    if not raw_acodec:
        return AudioCodec.NONE, None

    codec = raw_acodec.lower().strip()

    if codec in ("none", ""):
        return AudioCodec.NONE, raw_acodec

    # AAC
    if "aac" in codec or codec.startswith("mp4a.40.2"):
        return AudioCodec.AAC, raw_acodec

    # Opus
    if "opus" in codec:
        return AudioCodec.OPUS, raw_acodec

    # MP3
    if "mp3" in codec or codec.startswith("mp4a.40.34"):
        return AudioCodec.MP3, raw_acodec

    # Vorbis
    if "vorbis" in codec:
        return AudioCodec.VORBIS, raw_acodec

    # FLAC
    if "flac" in codec:
        return AudioCodec.FLAC, raw_acodec

    return AudioCodec.OTHER, raw_acodec
