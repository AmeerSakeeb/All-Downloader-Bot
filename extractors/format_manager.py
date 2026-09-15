"""Format inventory normalization, companion audio selection, and size calculation."""

import hashlib
import logging
import math
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
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


def _positive_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _optional_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> Optional[int]:
    number = _optional_number(value)
    return int(number) if number is not None and number >= 0 else None


def _text(value: Any) -> Optional[str]:
    return str(value) if value is not None and str(value).strip() else None


def _resource_identity(raw: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    """Fingerprint extractor URLs without exposing signed resources in UI/snapshots."""
    values = []
    for key in ("url", "manifest_url", "fragment_base_url"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            continue
        parsed = urlsplit(value)
        values.append(urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")))
    if not values:
        return None, {}
    digest = hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()
    return digest, {"extractor_resource_fingerprint": digest}


def _estimated_size_bytes(
    raw: Dict[str, Any], *, is_video: bool, is_audio: bool,
    duration: Any = None,
) -> Optional[int]:
    """Estimate stream bytes without changing exact-size semantics."""
    seconds = _positive_number(raw.get("duration")) or _positive_number(duration)
    if seconds is None:
        return None

    tbr = _positive_number(raw.get("tbr"))
    vbr = _positive_number(raw.get("vbr"))
    abr = _positive_number(raw.get("abr"))
    bitrate_kbps: Optional[float]
    if is_video and is_audio:
        bitrate_kbps = tbr or ((vbr + abr) if vbr is not None and abr is not None else None)
    elif is_video:
        bitrate_kbps = tbr or vbr
    else:
        bitrate_kbps = abr or tbr
    if bitrate_kbps is None:
        return None
    return max(1, int(round(seconds * bitrate_kbps * 1000 / 8)))


def normalize_format_inventory(
    raw_formats: List[Dict[str, Any]], *, duration: Any = None,
    extractor_identity: str | None = None,
) -> List[MediaFormat]:
    """
    Additive normalization of all extractor-provided formats.
    Preserves EVERY genuine downloadable format without discarding.
    """
    normalized_list: List[MediaFormat] = []

    for raw in raw_formats:
        if not isinstance(raw, dict):
            continue
        raw_format_id = raw.get("format_id")
        format_id = str(raw_format_id).strip() if raw_format_id is not None else ""
        if not format_id:
            continue

        if raw.get("has_drm") is True or raw.get("drm_family"):
            continue

        raw_vcodec = _text(raw.get("vcodec"))
        raw_acodec = _text(raw.get("acodec"))

        vcodec_norm, _ = normalize_video_codec(raw_vcodec)
        acodec_norm, _ = normalize_audio_codec(raw_acodec)

        is_video = vcodec_norm != VideoCodec.NONE
        is_audio = acodec_norm != AudioCodec.NONE

        # A missing codec is unknown, while the literal value "none" means the
        # extractor explicitly reported that stream type as absent. Retain
        # incomplete genuine records when other parsed media fields establish
        # their stream role.
        video_ext_hint = (_text(raw.get("video_ext")) or "").lower()
        audio_ext_hint = (_text(raw.get("audio_ext")) or "").lower()
        if raw_vcodec is None and (
            any(raw.get(key) is not None for key in ("width", "height", "resolution", "fps", "vbr"))
            or video_ext_hint not in {"", "none"}
        ):
            is_video = True
            vcodec_norm = VideoCodec.OTHER
        if raw_acodec is None and (
            any(raw.get(key) is not None for key in ("abr", "asr", "audio_channels"))
            or audio_ext_hint not in {"", "none"}
        ):
            is_audio = True
            acodec_norm = AudioCodec.OTHER
        if not is_video and not is_audio and raw_vcodec is None and raw_acodec is None:
            ext_hint = (_text(raw.get("ext")) or "").lower()
            if isinstance(raw.get("url"), str) and raw["url"].startswith(("http://", "https://")):
                if ext_hint in {"aac", "flac", "m4a", "mp3", "ogg", "opus", "wav"}:
                    is_audio = True
                    acodec_norm = AudioCodec.OTHER
                else:
                    is_video = True
                    vcodec_norm = VideoCodec.OTHER

        if not is_video and not is_audio:
            continue

        is_muxed = is_video and is_audio
        requires_separate_audio = is_video and not is_audio

        width = _optional_int(raw.get("width"))
        height = _optional_int(raw.get("height"))
        fps_value = _positive_number(raw.get("fps"))
        fps = round(fps_value, 2) if fps_value is not None else None

        res_label = parse_resolution_label(height, width)

        filesize = _optional_int(raw.get("filesize"))
        filesize_approx = _optional_int(raw.get("filesize_approx"))
        if filesize is None and filesize_approx is None:
            filesize_approx = _estimated_size_bytes(
                raw, is_video=is_video, is_audio=is_audio, duration=duration,
            )
        audio_lang = _text(raw.get("language") or raw.get("audio_language"))
        format_note = _text(raw.get("format_note")) or ""
        manifest_identity, source_identity = _resource_identity(raw)

        is_default = bool(raw.get("is_default") or "default" in format_note.lower())
        is_original = bool(raw.get("is_original") or "original" in format_note.lower())

        fmt = MediaFormat(
            format_id=format_id,
            extractor_identity=(
                _text(raw.get("extractor") or raw.get("extractor_key") or raw.get("ie_key"))
                or extractor_identity
            ),
            format_description=_text(raw.get("format")),
            is_video=is_video,
            is_audio=is_audio,
            is_muxed=is_muxed,
            requires_separate_audio=requires_separate_audio,
            vcodec_raw=raw_vcodec,
            vcodec_normalized=vcodec_norm,
            acodec_raw=raw_acodec,
            acodec_normalized=acodec_norm,
            video_codec_profile=_text(raw.get("vcodec_profile") or raw.get("video_codec_profile")),
            audio_codec_profile=_text(raw.get("acodec_profile") or raw.get("audio_codec_profile")),
            width=width,
            height=height,
            resolution_label=res_label,
            fps=fps,
            vbr=_optional_number(raw.get("vbr")),
            abr=_optional_number(raw.get("abr")),
            tbr=_optional_number(raw.get("tbr")),
            ext=_text(raw.get("ext")) or "unknown",
            protocol=_text(raw.get("protocol")),
            manifest_identity=manifest_identity,
            dynamic_range=_text(raw.get("dynamic_range")),
            quality=_optional_number(raw.get("quality")),
            preference=_optional_number(raw.get("preference")),
            source_preference=_optional_number(raw.get("source_preference")),
            language_preference=_optional_number(raw.get("language_preference")),
            audio_language=audio_lang,
            audio_is_default=is_default,
            audio_is_original=is_original,
            filesize=filesize,
            filesize_approx=filesize_approx,
            format_note=format_note,
            raw_metadata=raw,
            source_identity=source_identity,
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
