"""Reusable UI text builders and inline keyboard builders."""

from html import escape
import math
from urllib.parse import urlsplit, urlunsplit
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.models import (
    DownloadJob, FavoriteFormatRule, JobStatus, MediaFormat, MediaSession, UserSettings,
)
from services.favorites import FavoriteMatchResult


def build_media_info_text(session: MediaSession) -> str:
    """Build a rich text description of the media session."""
    duration_str = ""
    if session.duration:
        mins, secs = divmod(session.duration, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            duration_str = f"🕒 Duration: {hours}:{mins:02d}:{secs:02d}"
        else:
            duration_str = f"🕒 Duration: {mins:02d}:{secs:02d}"
    else:
        duration_str = "🕒 Duration: Unknown"

    uploader_str = f"👤 Creator: {escape(session.uploader)}" if session.uploader else "👤 Creator: Unknown"
    source_str = f"🌐 Source: {escape(session.extractor.upper())}"

    text = (
        f"🎬 <b>{escape(session.title)}</b>\n\n"
        f"{duration_str}\n"
        f"{uploader_str}\n"
        f"{source_str}\n\n"
        "Choose an exact source video format below to queue download:"
    )
    return text


def build_format_page_keyboard(
    session: MediaSession,
    page: int = 0,
    page_size: int = 5
) -> InlineKeyboardMarkup:
    """
    Build a paginated list of video formats with stable server-side keys.
    Callback structure:
    - Format selection: `fmt:{session_id}:{internal_key}`
    - Pagination: `page:{session_id}:{page_num}`
    - Cancellation: `cancel_session:{session_id}`
    """
    builder = InlineKeyboardBuilder()

    # Filter to video formats only (excluding audio-only formats for primary video panel)
    video_formats = [fmt for fmt in session.formats if fmt.is_video]

    # Deterministic sorting: resolution (descending), fps (descending), filesize (descending)
    def sort_key(fmt: MediaFormat):
        height = fmt.height or 0
        fps = fmt.fps or 0.0
        size = fmt.effective_size or 0
        return (height, fps, size)

    video_formats.sort(key=sort_key, reverse=True)

    total_formats = len(video_formats)
    total_pages = math.ceil(total_formats / page_size) if total_formats > 0 else 1

    # Clamp page
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_formats = video_formats[start_idx:end_idx]

    # Add format buttons
    for fmt in page_formats:
        res = fmt.resolution_label or f"{fmt.width}x{fmt.height}" if (fmt.width and fmt.height) else "Unknown Resolution"
        codec = fmt.vcodec_normalized.value if fmt.vcodec_normalized else "Other"
        fps_str = f"{int(fmt.fps)}fps" if fmt.fps else ""
        ext = fmt.ext.upper()
        size_str = fmt.format_size_display()

        # Build button label
        label = f"🎬 {res} {fps_str} · {codec} · {ext} ({size_str})"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"fmt:{session.session_id}:{fmt.internal_key}"
            )
        )

    # Add navigation row if multiple pages exist
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ Prev", callback_data=f"page:{session.session_id}:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"Page {page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="Next ➡️", callback_data=f"page:{session.session_id}:{page + 1}")
            )
        builder.row(*nav_buttons)

    # Add a global Cancel / Dismiss row
    builder.row(
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_session:{session.session_id}")
    )

    return builder.as_markup()


def build_progress_text(job: DownloadJob) -> str:
    """Build rich text progress indicators with throttling safe display."""
    headers = {
        JobStatus.QUEUED: "⏳ Queued",
        JobStatus.CLAIMED: "⏳ Preparing download",
        JobStatus.WAITING_RESOURCES: "⏳ Waiting for resources",
        JobStatus.DOWNLOADING_VIDEO: "⬇️ Downloading video",
        JobStatus.DOWNLOADING_AUDIO: "🎵 Downloading audio",
        JobStatus.MERGING: "🔧 Combining streams losslessly",
        JobStatus.UPLOADING: "⬆️ Uploading to Telegram",
        JobStatus.COMPLETED: "✅ Completed",
        JobStatus.FAILED: "❌ Failed",
        JobStatus.CANCELLED: "⏹ Cancelled",
    }
    header = headers.get(job.status, "Download status")
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        detail = job.error_message if job.status == JobStatus.FAILED else job.current_stage
        failure_headers = {
            "media_unavailable": "❌ Media unavailable",
            "authentication_required": "🔐 Authentication required",
            "drm_unsupported": "🛡 Unsupported protected media",
            "insufficient_disk": "💾 Not enough safe disk space",
            "exact_format_disappeared": "🎞 Exact format no longer available",
            "lossless_merge_unavailable": "🎞 Lossless merge unavailable",
            "delivery_size_exceeded": "📦 Telegram delivery limit exceeded",
        }
        if job.status == JobStatus.FAILED:
            header = failure_headers.get(job.error_category or "", header)
        suffix = "\n\nOriginal streams preserved. No compression or re-encoding." if job.status == JobStatus.COMPLETED else ""
        return f"<b>{header}</b>\n\n{escape(detail or '')}{suffix}"
    progress_bar_length = 15
    filled_length = int(round(progress_bar_length * (job.progress_pct / 100.0)))
    bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)

    speed_mb = job.speed_bytes_sec / (1024 * 1024) if job.speed_bytes_sec else 0.0
    downloaded_mb = job.downloaded_bytes / (1024 * 1024)
    total_mb = job.total_bytes / (1024 * 1024) if job.total_bytes else None

    eta_str = f"~{job.eta_seconds}s" if job.eta_seconds is not None else "Unknown"

    snapshot = job.video_format_snapshot or {}
    codec = snapshot.get("vcodec_normalized") or "Original source"
    quality = snapshot.get("resolution_label") or "Unknown quality"
    fps = snapshot.get("fps")
    media_line = f"{codec} · {quality}" + (f" · {fps:g} FPS" if isinstance(fps, (int, float)) else "")
    stage_number = {
        JobStatus.DOWNLOADING_VIDEO: "Stage 1/3",
        JobStatus.DOWNLOADING_AUDIO: "Stage 2/3",
        JobStatus.MERGING: "Stage 2/3",
        JobStatus.UPLOADING: "Stage 3/3",
    }.get(job.status, "Preparing")
    processing_note = (
        "\nVideo downloaded ✓\nAudio downloaded ✓\nCombining original streams losslessly.\nNo re-encoding is being performed."
        if job.status == JobStatus.MERGING else ""
    )
    text = (
        f"<b>{header}</b>\n\n"
        f"{escape(media_line)}\n\n{bar}\n{job.progress_pct:.1f}%\n\n"
        f"📋 Stage: {escape(job.current_stage)}\n"
        f"💾 Downloaded: {downloaded_mb:.1f} MB / {f'{total_mb:.1f} MB' if total_mb else 'Unknown'}\n"
        f"⚡ Speed: {speed_mb:.1f} MB/s\n"
        f"⏳ ETA: {eta_str}\n\n{stage_number}{processing_note}"
    )
    return text


def build_progress_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Keyboard for progress messages, showing a cancellation button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Cancel Download", callback_data=f"cancel_job:{job_id}")
    )
    return builder.as_markup()


def format_button_label(fmt: MediaFormat) -> str:
    codec = fmt.vcodec_normalized.value.replace("H.265 / HEVC", "H265").replace("H.264 / AVC", "H264")
    quality = fmt.resolution_label or (f"{fmt.height}p" if fmt.height else "Unknown")
    fps = f"{fmt.fps:g}fps" if fmt.fps else "FPS ?"
    return f"{codec} · {quality} {fps} · {fmt.format_size_display()}"


def stream_status_text(fmt: MediaFormat) -> str:
    if fmt.is_muxed:
        return "⚡ <b>Ready</b> — video and audio are already included"
    if not fmt.requires_separate_audio:
        return "⚡ <b>Ready</b> — the original source stream needs no merge"
    return (
        "🔧 <b>Merge required</b> — separate video and audio streams will be "
        "downloaded and combined losslessly"
    )


def build_preferred_media_text(
    session: MediaSession, result: FavoriteMatchResult, page: int = 0,
    page_size: int = 6,
) -> str:
    pages = max(1, math.ceil(len(result.formats) / page_size))
    page = max(0, min(page, pages - 1))
    visible_formats = result.formats[page * page_size:(page + 1) * page_size]
    videos = sum(1 for fmt in session.formats if fmt.is_video)
    lines = [
        "🎬 <b>Media Found</b>", "",
        f"<b>Title:</b> {escape(session.title)}",
        f"<b>Source:</b> {escape(session.extractor)}",
        f"<b>Duration:</b> {_duration(session.duration)}",
        f"<b>Video formats discovered:</b> {videos}", "",
        f"<b>Your preferred formats</b> · Page {page + 1} / {pages}",
    ]
    if not result.formats:
        lines.append("No enabled favorite rule has an exact match in this source.")
    for fmt in visible_formats:
        lines.extend(("", f"⭐ <b>{escape(format_button_label(fmt))}</b>", stream_status_text(fmt)))
    if result.total_combinations:
        available = result.total_combinations - result.unavailable_combinations
        lines.extend(("", f"{available} of {result.total_combinations} preferred combinations have an exact match."))
    lines.extend(("", "No compression, conversion or re-encoding will occur."))
    return "\n".join(lines)


def build_preferred_keyboard(
    session: MediaSession, result: FavoriteMatchResult, page: int = 0,
    page_size: int = 6,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    pages = max(1, math.ceil(len(result.formats) / page_size))
    page = max(0, min(page, pages - 1))
    for fmt in result.formats[page * page_size:(page + 1) * page_size]:
        builder.row(InlineKeyboardButton(
            text=format_button_label(fmt), callback_data=f"detail:{session.session_id}:{fmt.internal_key}"
        ))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀️", callback_data=f"preferred:{session.session_id}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                text="▶️", callback_data=f"preferred:{session.session_id}:{page + 1}"
            ))
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🎞 Browse All Formats", callback_data=f"all:{session.session_id}:0"))
    builder.row(
        InlineKeyboardButton(text="🎵 Audio Only", callback_data=f"audio:{session.session_id}"),
        InlineKeyboardButton(text="🖼 Thumbnail", callback_data=f"assets:{session.session_id}:thumb"),
    )
    builder.row(
        InlineKeyboardButton(text="ℹ️ Media Info", callback_data=f"info:{session.session_id}"),
        InlineKeyboardButton(text="⚙️ Settings", callback_data=f"settings:{session.session_id}"),
    )
    if session.subtitles:
        builder.row(InlineKeyboardButton(text="💬 Subtitles", callback_data=f"assets:{session.session_id}:sub"))
    builder.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_session:{session.session_id}"))
    return builder.as_markup()


def _duration(value: int | None) -> str:
    if value is None:
        return "Unknown"
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def build_format_details_text(
    fmt: MediaFormat, audio: MediaFormat | None, style: str = "rich",
    *, manual_audio_required: bool = False,
) -> str:
    if style == "compact":
        audio_note = (
            "\n\n🎵 Manual audio selection required. Expected download depends on selected audio."
            if manual_audio_required else ""
        )
        return (
            f"🎞 <b>Selected Format</b>\n\n{escape(format_button_label(fmt))}\n"
            f"{stream_status_text(fmt)}{audio_note}\n\nNo compression or re-encoding will occur."
        )
    total = fmt.effective_size
    exact = fmt.is_exact_size
    if audio:
        total = total + audio.effective_size if total is not None and audio.effective_size is not None else None
        exact = exact and audio.is_exact_size
    total_text = "Unknown" if total is None else ("" if exact else "~") + f"{total / 1024**2:.1f} MB"
    audio_text = "Manual audio selection required" if manual_audio_required else ("Already included" if fmt.is_muxed else (
        f"{audio.audio_language or 'Unknown language'} · {audio.acodec_normalized.value} · "
        f"{audio.abr:g} kbps" if audio and audio.abr else
        (f"{audio.audio_language or 'Unknown language'} · {audio.acodec_normalized.value}" if audio else "Unavailable")
    ))
    expected_text = "Depends on selected audio" if manual_audio_required else total_text
    audio_size_text = (
        "Choose an exact stream" if manual_audio_required else
        (audio.format_size_display() if audio else ('Included' if fmt.is_muxed else 'Unknown'))
    )
    dimensions = f"{fmt.width}×{fmt.height}" if fmt.width and fmt.height else "Unknown dimensions"
    fps_text = f"{fmt.fps:g} FPS" if fmt.fps is not None else "Unknown FPS"
    return (
        "🎞 <b>Selected Format</b>\n\n"
        f"<b>Quality</b>\n{escape(fmt.resolution_label or 'Unknown')}\n"
        f"{dimensions}\n{fps_text}\n\n"
        f"<b>Video</b>\nCodec: {escape(fmt.vcodec_normalized.value)}\n"
        f"Container: {escape(fmt.ext.upper())}\n"
        f"Bitrate: {f'~{fmt.vbr:g} kbps' if fmt.vbr else 'Unknown'}\n"
        f"Format ID: <code>{escape(fmt.format_id)}</code>\n\n"
        f"<b>Audio</b>\n{escape(audio_text)}\n\n"
        f"<b>Size</b>\nVideo: {fmt.format_size_display()}\n"
        f"Audio: {audio_size_text}\n"
        f"Expected download: {expected_text}\n\n<b>Processing</b>\n{stream_status_text(fmt)}\n\n"
        "The original streams are preserved. No compression, conversion or re-encoding will occur."
    )


def build_format_details_keyboard(
    session: MediaSession, fmt: MediaFormat, *, back: str = "preferred",
    manual_audio_required: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬇️ Download", callback_data=f"fmt:{session.session_id}:{fmt.internal_key}"))
    if fmt.requires_separate_audio:
        builder.row(InlineKeyboardButton(
            text="🎵 Choose Audio" if manual_audio_required else "🎵 Audio Options",
            callback_data=f"audopts:{session.session_id}:{fmt.internal_key}",
        ))
    back_data = f"preferred:{session.session_id}" if back == "preferred" else f"all:{session.session_id}:0"
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data=back_data))
    return builder.as_markup()


def filtered_video_formats(session: MediaSession, filters: dict[str, object]) -> list[MediaFormat]:
    rule = FavoriteFormatRule(
        user_id=session.user_id,
        codecs=filters.get("codec") or ["any"],
        resolutions=filters.get("resolution") or ["any"],
        fps_values=filters.get("fps") or ["any"],
        containers=filters.get("container") or ["any"],
    )
    from services.favorites import FavoriteMatcher
    values = [fmt for fmt in session.formats if fmt.is_video and FavoriteMatcher.matches_rule(fmt, rule)]
    query = str(filters.get("query") or "").strip().lower()
    if query:
        values = [fmt for fmt in values if query in " ".join((
            fmt.format_id, fmt.internal_key, fmt.vcodec_normalized.value,
            fmt.vcodec_raw or "", fmt.resolution_label or "", str(fmt.height or ""),
            str(fmt.fps or ""), fmt.ext,
        )).lower()]
    return sorted(values, key=lambda fmt: (-(fmt.height or 0), -(fmt.fps or 0), fmt.format_id))


def build_all_formats_keyboard(
    session: MediaSession, filters: dict[str, object], page: int = 0, page_size: int = 6
) -> InlineKeyboardMarkup:
    values = filtered_video_formats(session, filters)
    pages = max(1, math.ceil(len(values) / page_size))
    page = max(0, min(page, pages - 1))
    builder = InlineKeyboardBuilder()
    for fmt in values[page * page_size:(page + 1) * page_size]:
        builder.row(InlineKeyboardButton(
            text=format_button_label(fmt), callback_data=f"adetail:{session.session_id}:{fmt.internal_key}"
        ))
    builder.row(
        InlineKeyboardButton(text="Codec", callback_data=f"filter:{session.session_id}:codec"),
        InlineKeyboardButton(text="Resolution", callback_data=f"filter:{session.session_id}:resolution"),
    )
    builder.row(
        InlineKeyboardButton(text="FPS", callback_data=f"filter:{session.session_id}:fps"),
        InlineKeyboardButton(text="Container", callback_data=f"filter:{session.session_id}:container"),
    )
    builder.row(InlineKeyboardButton(text="🔎 Search", callback_data=f"search:{session.session_id}"))
    builder.row(InlineKeyboardButton(text="Reset filters", callback_data=f"freset:{session.session_id}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"all:{session.session_id}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"all:{session.session_id}:{page+1}"))
    builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Preferred", callback_data=f"preferred:{session.session_id}"))
    return builder.as_markup()


def build_filter_keyboard(session_id: str, category: str, selected: list[str]) -> InlineKeyboardMarkup:
    choices = {
        "codec": ["any", "h265", "h264", "vp9", "av1", "other"],
        "resolution": ["any", "2160", "1440", "1080", "720", "480", "360", "other"],
        "fps": ["any", "best", "60", "30", "24", "other"],
        "container": ["any", "mp4", "webm", "mkv-compatible", "other"],
    }[category]
    builder = InlineKeyboardBuilder()
    for value in choices:
        mark = "☑" if value in selected else "☐"
        builder.button(text=f"{mark} {value}", callback_data=f"ftoggle:{session_id}:{category}:{value}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="✅ Apply", callback_data=f"all:{session_id}:0"))
    return builder.as_markup()


def build_audio_keyboard(session: MediaSession, video_key: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fmt in sorted(
        (item for item in session.formats if item.is_audio and not item.is_video),
        key=lambda item: (-(item.abr or item.tbr or 0), item.format_id),
    ):
        label = f"{fmt.audio_language or 'Unknown'} · {fmt.acodec_normalized.value} · {fmt.format_size_display()}"
        callback = (
            f"fmta:{session.session_id}:{video_key}:{fmt.internal_key}"
            if video_key else f"aonly:{session.session_id}:{fmt.internal_key}"
        )
        builder.row(InlineKeyboardButton(text=label, callback_data=callback))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"preferred:{session.session_id}"))
    return builder.as_markup()


def build_settings_text(settings: UserSettings) -> str:
    return (
        "⚙️ <b>Downloader Settings</b>\n\n"
        f"Send mode: <b>{'Telegram Video' if settings.send_mode == 'video' else 'File / Document'}</b>\n"
        f"Matching: <b>{settings.matching_strategy.value.replace('_', ' ').title()}</b>\n"
        f"Automatic audio: <b>{'Source/default' if settings.automatic_audio else 'Manual when required'}</b>\n"
        f"Format details: <b>{settings.detail_style.value.title()}</b>"
    )


def build_settings_keyboard(
    settings: UserSettings, media_session_id: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    def setting_callback(category: str, value: str) -> str:
        base = f"set:{category}:{value}"
        return f"{base}:{media_session_id}" if media_session_id else base
    builder.row(
        InlineKeyboardButton(text=("● Video" if settings.send_mode == "video" else "○ Video"), callback_data=setting_callback("send", "video")),
        InlineKeyboardButton(text=("● Document" if settings.send_mode == "document" else "○ Document"), callback_data=setting_callback("send", "document")),
    )
    builder.row(InlineKeyboardButton(text="⭐ Favorite Formats", callback_data="favorites"))
    for value, label in (("best_quality", "Best quality"), ("smallest_file", "Smallest file"), ("prefer_ready", "Prefer ready/muxed")):
        builder.row(InlineKeyboardButton(
            text=("● " if settings.matching_strategy.value == value else "○ ") + label,
            callback_data=setting_callback("strategy", value),
        ))
    builder.row(
        InlineKeyboardButton(text=("● Rich" if settings.detail_style.value == "rich" else "○ Rich"), callback_data=setting_callback("detail", "rich")),
        InlineKeyboardButton(text=("● Compact" if settings.detail_style.value == "compact" else "○ Compact"), callback_data=setting_callback("detail", "compact")),
    )
    builder.row(InlineKeyboardButton(
        text=("● Automatic audio" if settings.automatic_audio else "○ Automatic audio"),
        callback_data=setting_callback("auto_audio", "toggle"),
    ))
    if media_session_id:
        builder.row(InlineKeyboardButton(
            text="🔙 Back to Media", callback_data=f"preferred:{media_session_id}:0"
        ))
    return builder.as_markup()


def build_favorites_text(rules: list[FavoriteFormatRule]) -> str:
    lines = ["⭐ <b>Favorite Format Rules</b>"]
    for index, rule in enumerate(rules, 1):
        state = "✅" if rule.enabled else "⏸"
        lines.extend(("", f"{index}. {state} <b>{escape(rule.name)}</b>",
                      f"Codecs: {escape(', '.join(rule.codecs))}",
                      f"Resolutions: {escape(', '.join(rule.resolutions))}",
                      f"FPS: {escape(', '.join(rule.fps_values))}",
                      f"Containers: {escape(', '.join(rule.containers))}"))
    return "\n".join(lines)


def build_favorites_keyboard(rules: list[FavoriteFormatRule]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, rule in enumerate(rules, 1):
        builder.row(InlineKeyboardButton(text=f"✏️ Rule {index}", callback_data=f"fedit:{rule.rule_id}"))
    builder.row(InlineKeyboardButton(text="➕ Add Rule", callback_data="fadd"))
    builder.row(InlineKeyboardButton(text="🔙 Settings", callback_data="settings"))
    return builder.as_markup()


def build_rule_editor(rule: FavoriteFormatRule) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in ("codec", "resolution", "fps", "container"):
        builder.row(InlineKeyboardButton(text=f"Edit {category.title()}", callback_data=f"frcat:{rule.rule_id}:{category}"))
    builder.row(
        InlineKeyboardButton(text="📋 Duplicate", callback_data=f"frop:{rule.rule_id}:dup"),
        InlineKeyboardButton(text="✅/⏸", callback_data=f"frop:{rule.rule_id}:toggle"),
    )
    builder.row(
        InlineKeyboardButton(text="⬆️", callback_data=f"frop:{rule.rule_id}:up"),
        InlineKeyboardButton(text="⬇️", callback_data=f"frop:{rule.rule_id}:down"),
        InlineKeyboardButton(text="🗑", callback_data=f"frop:{rule.rule_id}:delete"),
    )
    builder.row(InlineKeyboardButton(text="🔙 Rules", callback_data="favorites"))
    return builder.as_markup()


def build_rule_choices(rule: FavoriteFormatRule, category: str) -> InlineKeyboardMarkup:
    field = {"codec": "codecs", "resolution": "resolutions", "fps": "fps_values", "container": "containers"}[category]
    selected = getattr(rule, field)
    choices = {
        "codec": ["any", "h265", "h264", "vp9", "av1", "other"],
        "resolution": ["any", "2160", "1440", "1080", "720", "480", "360", "other"],
        "fps": ["any", "best", "60", "30", "24", "other"],
        "container": ["any", "mp4", "webm", "mkv-compatible", "other"],
    }[category]
    builder = InlineKeyboardBuilder()
    for value in choices:
        builder.button(
            text=("☑ " if value in selected else "☐ ") + value,
            callback_data=f"frtoggle:{rule.rule_id}:{category}:{value}",
        )
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="✅ Done", callback_data=f"fedit:{rule.rule_id}"))
    return builder.as_markup()


def build_assets_keyboard(session: MediaSession, kind: str) -> InlineKeyboardMarkup:
    values = session.thumbnails if kind == "thumb" else session.subtitles
    builder = InlineKeyboardBuilder()
    for asset in values[:20]:
        if kind == "thumb":
            label = f"{asset.width or '?'}×{asset.height or '?'} · {asset.ext.upper()}"
        else:
            source = "Auto" if asset.autogenerated else "Creator"
            label = f"{asset.language or 'Unknown'} · {source} · {asset.ext.upper()}"
        builder.row(InlineKeyboardButton(
            text=label, callback_data=f"asset:{session.session_id}:{asset.asset_id}"
        ))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"preferred:{session.session_id}"))
    return builder.as_markup()


def build_collection_text(session: MediaSession) -> str:
    title = (
        "🔗 <b>Batch URLs</b>" if session.session_kind == "batch" else
        "📚 <b>Multi-media Post</b>" if session.session_kind == "multimedia" else
        "📚 <b>Playlist Detected</b>"
    )
    lines = [title, "", f"<b>Title:</b> {escape(session.title)}", f"Items loaded: {len(session.items)}"]
    if session.collection_truncated:
        lines.append("The collection was capped at the configured safe browse limit.")
    for index, item in enumerate(session.items[:20], 1):
        icon = "🖼" if item.kind == "image" else "🎞"
        quality = f" · up to {item.max_height}p" if item.max_height else ""
        lines.append(f"{index}. {icon} {escape(item.title)}{quality}")
    return "\n".join(lines)


def build_collection_keyboard(session: MediaSession, page: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    page_size = 10
    values = session.items[page * page_size:(page + 1) * page_size]
    for index, item in enumerate(values, page * page_size + 1):
        icon = "🖼" if item.kind == "image" else "🎞"
        builder.button(text=f"{index} {icon}", callback_data=f"item:{session.session_id}:{item.item_id}")
    builder.adjust(3)
    pages = max(1, math.ceil(len(session.items) / page_size))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"collection:{session.session_id}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"collection:{session.session_id}:{page+1}"))
    builder.row(*nav)
    if session.session_kind == "playlist":
        builder.row(
            InlineKeyboardButton(text="First 5", callback_data=f"items:{session.session_id}:5"),
            InlineKeyboardButton(text="First 10", callback_data=f"items:{session.session_id}:10"),
        )
    builder.row(InlineKeyboardButton(
        text="🔗 Process Individually" if session.session_kind == "batch" else "☑ Select Items",
        callback_data=f"iselect:{session.session_id}",
    ))
    if session.session_kind == "multimedia":
        builder.row(InlineKeyboardButton(
            text="📦 Download All Media", callback_data=f"allmedia:{session.session_id}"
        ))
    builder.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_session:{session.session_id}"))
    return builder.as_markup()


def build_item_selection_keyboard(
    session: MediaSession, selected: list[str], page: int = 0,
    page_size: int = 15,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    pages = max(1, math.ceil(len(session.items) / page_size))
    page = max(0, min(page, pages - 1))
    visible = session.items[page * page_size:(page + 1) * page_size]
    for index, item in enumerate(visible, page * page_size + 1):
        mark = "☑" if item.item_id in selected else "☐"
        builder.button(
            text=f"{mark} {index}", callback_data=f"itoggle:{session.session_id}:{item.item_id}:{page}"
        )
    builder.adjust(3)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀️", callback_data=f"iselect:{session.session_id}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                text="▶️", callback_data=f"iselect:{session.session_id}:{page + 1}"
            ))
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="▶️ Process selected", callback_data=f"iprocess:{session.session_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"collection:{session.session_id}"))
    return builder.as_markup()


def build_media_info_details(session: MediaSession) -> str:
    videos = sum(1 for fmt in session.formats if fmt.is_video)
    audio = sum(1 for fmt in session.formats if fmt.is_audio and not fmt.is_video)
    description = (session.description or "Unknown").replace("\n", " ")[:500]
    return (
        "ℹ️ <b>Media Info</b>\n\n"
        f"<b>Title:</b> {escape(session.title)}\n"
        f"<b>Creator:</b> {escape(session.uploader or 'Unknown')}\n"
        f"<b>Source:</b> {escape(session.extractor)}\n"
        f"<b>Duration:</b> {_duration(session.duration)}\n"
        f"<b>Upload date:</b> {escape(session.upload_date or 'Unknown')}\n"
        f"<b>Media ID:</b> {escape(session.media_id or 'Unknown')}\n"
        f"<b>Canonical URL:</b> {escape(_safe_display_url(session.canonical_url or session.url))}\n"
        f"<b>Formats:</b> {videos} video, {audio} audio\n\n"
        f"<b>Description:</b> {escape(description)}"
    )


def _safe_display_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def build_admin_status_text(
    sample: dict, stats: dict[str, int], limits: dict[str, int], mode: str, paused: bool,
    disk_free: int,
) -> str:
    cpu = sample["cpu"]
    memory = sample["memory"]
    return (
        "🖥 <b>Server</b>\n\n"
        f"CPU: {float(cpu['load_percent']):.0f}%\n"
        f"Memory available: {int(memory['available']) / 1024**3:.2f} GB\n"
        f"Disk free: {disk_free / 1024**3:.2f} GB\n\n"
        "📥 <b>Queue</b>\n\n"
        f"Active: {stats['active']}\nWaiting: {stats['waiting']}\nFailed: {stats['failed']}\n\n"
        "⚙️ <b>Resource Governor</b>\n\n"
        f"Mode: {escape(mode)}\nAdmissions: {'Paused' if paused else 'Running'}\n"
        f"Capacity — extraction {limits['extraction']}, download {limits['download']}, "
        f"merge {limits['merge']}, upload {limits['upload']}\n\n"
        "🤖 <b>Bot</b>\n\n"
        f"Successful jobs: {stats['successful']}\nFailures: {stats['failed']}\n"
        f"Cache hits: {stats['cache_hits']}"
    )


def build_admin_keyboard(paused: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="▶️ Resume New Jobs" if paused else "⏸ Pause New Jobs",
        callback_data="admin:resume" if paused else "admin:pause",
    ))
    builder.row(
        InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
        InlineKeyboardButton(text="⚙️ Resources", callback_data="admin:resources"),
    )
    builder.row(InlineKeyboardButton(text="🧹 Cleanup expired sessions", callback_data="admin:cleanup"))
    return builder.as_markup()


def build_resource_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in (
        ("auto-shared", "Auto — Shared VM"),
        ("auto-dedicated", "Auto — Dedicated VM"),
        ("manual", "Manual"),
    ):
        builder.row(InlineKeyboardButton(
            text=("● " if current == value else "○ ") + label,
            callback_data=f"resource:{value}",
        ))
    builder.row(InlineKeyboardButton(text="🔙 Admin", callback_data="admin:status"))
    return builder.as_markup()


def build_users_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users[:40]:
        state = "✅" if user["is_allowed"] else "🛑"
        role = "👑" if user["is_admin"] else "👤"
        builder.row(InlineKeyboardButton(
            text=f"{state} {role} {user['user_id']}", callback_data=f"user:{user['user_id']}:view"
        ))
    builder.row(InlineKeyboardButton(text="➕ Add with /allow ID", callback_data="noop"))
    builder.row(InlineKeyboardButton(text="🔙 Admin", callback_data="admin:status"))
    return builder.as_markup()


def build_user_details(user: dict, stats: dict[str, int]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "👤 <b>User Status</b>\n\n"
        f"ID: <code>{user['user_id']}</code>\n"
        f"Role: {'Administrator' if user['is_admin'] else 'User'}\n"
        f"Access: {'Allowed' if user['is_allowed'] else 'Suspended'}\n"
        f"Jobs: {stats['total']} total, {stats['active']} active, "
        f"{stats['completed']} completed, {stats['failed']} failed"
    )
    builder = InlineKeyboardBuilder()
    if not user["is_admin"]:
        builder.row(InlineKeyboardButton(
            text="🛑 Suspend" if user["is_allowed"] else "✅ Allow",
            callback_data=f"user:{user['user_id']}:toggle",
        ))
    builder.row(InlineKeyboardButton(text="🔙 Users", callback_data="admin:users"))
    return text, builder.as_markup()
