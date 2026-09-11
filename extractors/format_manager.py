"""Format inventory normalization, companion audio selection, and size calculation."""

import logging
from typing import Any, Dict, List, Optional, Tuple
from core.models import AudioCodec, MediaFormat, VideoCodec
from extractors.codec_normalizer import normalize_audio_codec, normalize_video_codec

logger = logging.getLogger(__name__)


def parse_resolution_label(height: Optional[int], width: Optional[int]) -> Optional[str]:
    """Generate clean resolution labels like 1080p, 720p, 4K."""
    if not height and not width:
        return None
    h = height or 0
    w = width or 0
    dim = max(h, w) if h == 0 or w == 0 else min(h, w)

    if dim >= 2160:
        return "2160p (4K)"
    if dim >= 1440:
        return "1440p (2K)"
    if dim >= 1080:
        return "1080p"
    if dim >= 720:
        return "720p"
    if dim >= 480:
        return "480p"
    if dim >= 360:
        return "360p"
    if dim >= 240:
        return "240p"
    if dim > 0:
        return f"{dim}p"
    return None


def normalize_format_inventory(raw_formats: List[Dict[str, Any]]) -> List[MediaFormat]:
    """
    Additive normalization of all extractor-provided formats.
    Preserves EVERY genuine downloadable format without discarding.
    """
    normalized_list: List[MediaFormat] = []

    for raw in raw_formats:
        format_id = str(raw.get("format_id", ""))
        if not format_id:
            continue

        raw_vcodec = raw.get("vcodec")
        raw_acodec = raw.get("acodec")

        vcodec_norm, _ = normalize_video_codec(raw_vcodec)
        acodec_norm, _ = normalize_audio_codec(raw_acodec)

        is_video = vcodec_norm != VideoCodec.NONE
        is_audio = acodec_norm != AudioCodec.NONE

        if not is_video and not is_audio:
            continue

        is_muxed = is_video and is_audio
        requires_separate_audio = is_video and not is_audio

        width = raw.get("width")
        height = raw.get("height")
        fps = raw.get("fps")
        if fps:
            try:
                fps = round(float(fps), 2)
            except (ValueError, TypeError):
                fps = None

        res_label = parse_resolution_label(height, width)

        filesize = raw.get("filesize")
        filesize_approx = raw.get("filesize_approx")
        audio_lang = raw.get("language") or raw.get("audio_language")
        format_note = raw.get("format_note") or ""

        is_default = bool(raw.get("is_default") or "default" in format_note.lower())
        is_original = bool(raw.get("is_original") or "original" in format_note.lower())

        fmt = MediaFormat(
            format_id=format_id,
            is_video=is_video,
            is_audio=is_audio,
            is_muxed=is_muxed,
            requires_separate_audio=requires_separate_audio,
            vcodec_raw=raw_vcodec,
            vcodec_normalized=vcodec_norm,
            acodec_raw=raw_acodec,
            acodec_normalized=acodec_norm,
            width=width,
            height=height,
            resolution_label=res_label,
            fps=fps,
            vbr=raw.get("vbr"),
            abr=raw.get("abr"),
            tbr=raw.get("tbr"),
            ext=raw.get("ext", "mp4"),
            protocol=raw.get("protocol"),
            audio_language=audio_lang,
            audio_is_default=is_default,
            audio_is_original=is_original,
            filesize=filesize,
            filesize_approx=filesize_approx,
            format_note=format_note,
            raw_metadata=raw
        )
        normalized_list.append(fmt)

    return normalized_list

def select_default_audio(
    available_formats: List[MediaFormat],
    video_format: MediaFormat
) -> Optional[MediaFormat]:
    """
    Deterministically rank and select the best companion audio track for a video-only format.
    Respects original/source tracks, filters commentary/narration, prefers English among
    otherwise neutral candidates, and optimizes for compatible codecs and higher bitrates.
    """
    if video_format.is_muxed or not video_format.requires_separate_audio:
        return None

    audio_candidates = [
        f for f in available_formats
        if f.is_audio and not f.is_video
    ]

    if not audio_candidates:
        return None

    def rank_score(fmt: MediaFormat) -> Tuple[int, int, int, int, float]:
        lang = (fmt.audio_language or "").lower()
        note = (fmt.format_note or "").lower()

        is_commentary = any(w in note for w in ("commentary", "description", "descriptive", "narration"))
        penalty = -1000 if is_commentary else 0

        original_score = 500 if (fmt.audio_is_original or "original" in note) else 0
        default_score = 300 if (fmt.audio_is_default or "default" in note) else 0

        is_english = lang in ("en", "eng", "en-us", "en-gb", "english")
        lang_score = 100 if is_english else (20 if not lang else 0)

        codec_score = 0
        if fmt.acodec_normalized == AudioCodec.AAC:
            codec_score = 50
        elif fmt.acodec_normalized == AudioCodec.OPUS:
            codec_score = 40
        elif fmt.acodec_normalized == AudioCodec.FLAC:
            codec_score = 30
        elif fmt.acodec_normalized == AudioCodec.MP3:
            codec_score = 20

        bitrate = fmt.abr or fmt.tbr or 0.0
        return (penalty, original_score, default_score, lang_score, codec_score + bitrate)

    ranked = sorted(audio_candidates, key=rank_score, reverse=True)
    return ranked[0] if ranked else None


def calculate_total_download_size(
    video_format: MediaFormat,
    audio_format: Optional[MediaFormat] = None
) -> Tuple[Optional[int], bool]:
    """
    Calculate the total expected download size.
    Returns: (total_bytes, is_exact)
    """
    if video_format.is_muxed or not audio_format:
        return video_format.effective_size, video_format.is_exact_size

    v_size = video_format.effective_size
    a_size = audio_format.effective_size

    if v_size is None or a_size is None:
        return None, False

    total = v_size + a_size
    is_exact = video_format.is_exact_size and audio_format.is_exact_size
    return total, is_exact
