"""Reusable UI text builders and inline keyboard builders."""

from html import escape
import math
from urllib.parse import urlsplit, urlunsplit
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.models import (
    DownloadJob, FavoriteFormatRule, JobStatus, MediaFormat, MediaItem, MediaSession,
    UserSettings, whole_duration_seconds,
)
from services.favorites import FavoriteMatchResult
from services.telegram_capabilities import TelegramCapabilities
from extractors.format_manager import calculate_total_download_size, select_default_audio


def _short(value: str | None, limit: int = 72, fallback: str = "Untitled media") -> str:
    text = " ".join((value or fallback).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _source_label(value: str | None) -> str:
    host = (urlsplit(value or "").hostname or "").lower().removeprefix("www.")
    known = {
        "facebook.com": "Facebook", "fb.watch": "Facebook",
        "instagram.com": "Instagram", "tiktok.com": "TikTok",
        "youtube.com": "YouTube", "youtu.be": "YouTube",
    }
    for domain, label in known.items():
        if host == domain or host.endswith("." + domain):
            return label
    return (host.split(".")[0].replace("-", " ").title() if host else "Media")


def build_media_info_text(session: MediaSession) -> str:
    """Build a rich text description of the media session."""
    display_seconds = whole_duration_seconds(session.duration)
    if display_seconds is not None:
        mins, secs = divmod(display_seconds, 60)
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
        fps_str = f" · {fmt.fps:g} FPS" if fmt.fps else ""
        ext = fmt.ext.upper()
        size_str = fmt.format_size_display()

        # Build button label
        label = f"🎞 {res}{fps_str} · {codec} · {ext} · {size_str}"
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
                InlineKeyboardButton(text="◀ Previous", callback_data=f"page:{session.session_id}:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"Page {page + 1} of {total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="Next ▶", callback_data=f"page:{session.session_id}:{page + 1}")
            )
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))

    return builder.as_markup()


def build_progress_text(job: DownloadJob) -> str:
    """Build truthful stage and progress indicators.

    ``progress_pct`` is current downloader-stage progress, never overall job
    progress. Merge and upload progress is intentionally stage based.
    """
    if job.status == JobStatus.FAILED:
        return build_error_text(job.error_category)
    if job.status == JobStatus.COMPLETED:
        return (
            "✅ <b>Download complete</b>\n\n"
            "Original source streams were delivered without video re-encoding."
        )
    if job.status == JobStatus.CANCELLED:
        return "⏹ <b>Download cancelled</b>\n\nNo further processing will occur."
    snapshot = job.video_format_snapshot or {}
    codec = snapshot.get("vcodec_normalized") or "Original source"
    quality = snapshot.get("resolution_label") or "Unknown quality"
    media_line = f"{quality} · {codec}"
    if job.status in {JobStatus.DOWNLOADING_VIDEO, JobStatus.DOWNLOADING_AUDIO}:
        pct = max(0.0, min(100.0, job.progress_pct))
        filled = int(round(12 * pct / 100.0))
        bar = "█" * filled + "░" * (12 - filled)
        title = "🎵 <b>Downloading original audio</b>" if job.status == JobStatus.DOWNLOADING_AUDIO else "⬇️ <b>Downloading original video</b>"
        lines = [title, "", escape(media_line), f"{bar}  <b>{pct:.1f}%</b>"]
        if job.total_bytes:
            lines.append(
                f"Downloaded: {job.downloaded_bytes / 1024**2:.1f} / "
                f"{job.total_bytes / 1024**2:.1f} MB"
            )
        if job.speed_bytes_sec:
            lines.append(f"Speed: {job.speed_bytes_sec / 1024**2:.1f} MB/s")
        if job.eta_seconds is not None:
            lines.append(f"ETA: ~{job.eta_seconds}s")
        return "\n".join(lines)
    if job.status == JobStatus.MERGING:
        return (
            "🔧 <b>Finalizing video</b>\n\nVideo and audio downloads are complete.\n\n"
            "Combining the original streams without re-encoding."
        )
    if job.status == JobStatus.UPLOADING:
        return "⬆️ <b>Sending to Telegram</b>\n\nDownload and processing are complete."
    if job.status == JobStatus.WAITING_RESOURCES:
        stage = (job.current_stage or "").lower()
        if "merge" in stage or "final" in stage:
            title = "⏳ <b>Waiting for a safe finalizing slot</b>"
            detail = "The source streams are downloaded and saved. Finalizing will continue automatically."
        elif "upload" in stage or "telegram" in stage or "send" in stage:
            title = "⏳ <b>Waiting to send to Telegram</b>"
            detail = "Processing is complete. Delivery will continue automatically."
        else:
            title = "⏳ <b>Waiting for a safe download slot</b>"
            detail = "Your download is saved in the queue. It will continue automatically."
        return (
            f"{title}\n\n{detail}\n\nYou do not need to resend the link."
        )
    return (
        "⏳ <b>Download queued</b>\n\n"
        "It will start automatically.\nYou do not need to resend the link."
    )


def build_error_text(category: str | None) -> str:
    messages = {
        "site_access_challenge": (
            "🛡 <b>This website blocked the server request</b>\n\n"
            "The link may still be valid.\n\n"
            "The website refused automated access from this server.\n\n"
            "Try again later, or use an operator-authorized site session if one is available."
        ),
        "authentication_required": (
            "🔐 <b>This website requires sign-in</b>\n\n"
            "The link may still be valid, but the site is refusing anonymous server access.\n\n"
            "The bot cannot bypass account or access restrictions."
        ),
        "unsupported_url": (
            "🔗 <b>This link is not supported</b>\n\n"
            "The bot could not identify downloadable media at this URL.\n\n"
            "Check the link or try another public media page."
        ),
        "media_unavailable": (
            "🚫 <b>Media unavailable</b>\n\n"
            "The media may have been deleted, made private, or restricted.\n\n"
            "Check that the link opens publicly and try again."
        ),
        "extraction_timeout": (
            "⏱ <b>Analysis took too long</b>\n\n"
            "The website did not respond in time.\n\nYou can safely try again."
        ),
        "extractor_compatibility_failure": (
            "⚠️ <b>This video could not be analyzed</b>\n\n"
            "The website changed how this media is provided.\n\n"
            "Your link may still be valid. You can try again later."
        ),
        "no_formats": (
            "⚠️ <b>No downloadable source qualities were found</b>\n\n"
            "The website returned no usable, unprotected source formats."
        ),
        "network_failure": (
            "🌐 <b>The source connection failed</b>\n\n"
            "The link may still be valid. Please try again later."
        ),
        "geo_restricted": (
            "🌍 <b>This media is unavailable in the bot's region</b>\n\n"
            "The bot does not bypass geographic access restrictions."
        ),
        "drm_unsupported": (
            "🔒 <b>Protected media cannot be downloaded</b>\n\n"
            "The link may be valid, but DRM, paywalls, and private access controls are not bypassed."
        ),
        "insufficient_disk": (
            "💾 <b>Not enough safe storage</b>\n\nThe link and selected quality may still be valid.\n\n"
            "The bot protected its storage reserve. Try again later or choose a smaller original source quality."
        ),
        "exact_format_disappeared": (
            "🎞 <b>The selected source quality is no longer available</b>\n\nThe link may still be valid.\n\n"
            "The website changed its format list. Analyze the link again and choose another source quality."
        ),
        "lossless_merge_unavailable": (
            "🔧 <b>The original streams could not be combined</b>\n\nThe link may still be valid.\n\n"
            "A lossless combination was not possible, so the bot stopped instead of re-encoding the video."
        ),
        "delivery_size_exceeded": (
            "📦 <b>The result is too large for Telegram delivery</b>\n\nThe link is probably valid.\n\n"
            "Choose a smaller original source quality and try again."
        ),
        "telegram_delivery_unavailable": (
            "📦 <b>Large-file Telegram delivery is temporarily unavailable</b>\n\n"
            "The configured Local Bot API endpoint is not ready. No cloud upload was attempted."
        ),
        "telegram_delivery_failed": (
            "📤 <b>Telegram could not receive the download</b>\n\nThe link is probably valid and processing finished.\n\n"
            "Check your downloads or try again later."
        ),
    }
    return messages.get(category, (
        "⚠️ <b>The download could not be completed</b>\n\n"
        "The link may still be valid. Please try again later or choose Help for guidance."
    ))


def build_progress_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Keyboard for progress messages, showing a cancellation button."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Cancel download", callback_data=f"cancel_job:{job_id}")
    )
    return builder.as_markup()


def build_error_keyboard(job: DownloadJob) -> InlineKeyboardMarkup:
    """Offer only safe recovery actions for a categorized terminal failure."""
    builder = InlineKeyboardBuilder()
    if job.error_category == "exact_format_disappeared":
        builder.row(
            InlineKeyboardButton(text="📥 Analyze another link", callback_data="home:new"),
            InlineKeyboardButton(
                text="🎞 Choose another format",
                callback_data=f"preferred:{job.media_session_id}",
            ),
        )
    elif job.error_category in {
        "authentication_required", "drm_unsupported", "site_access_challenge",
        "media_unavailable", "extraction_timeout", "extractor_compatibility_failure",
        "no_formats", "network_failure", "geo_restricted",
    }:
        builder.row(
            InlineKeyboardButton(text="📥 Try another link", callback_data="home:new"),
            InlineKeyboardButton(text="❓ Failure help", callback_data="help:failures"),
        )
    elif job.error_category == "telegram_delivery_failed":
        builder.row(InlineKeyboardButton(text="📋 View Queue", callback_data="queue:mine"))
    builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def build_home_text(
    *, active: int, waiting: int, download_target: int, mode: str,
    max_links: int = 4,
) -> str:
    return (
        "👋 <b>Media Downloader</b>\n\n"
        f"Send a video link, or paste up to {max_links} links together.\n\n"
        "<b>Your downloads</b>\n"
        f"⬇️ Active: <b>{active}</b>\n"
        f"⏳ Waiting: <b>{waiting}</b>\n\n"
        f"Up to {download_target} download{'s' if download_target != 1 else ''} at a time right now.\n\n"
        "Original source quality\nNo video re-encoding"
    )


def build_home_keyboard(*, is_admin: bool, max_links: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📥 Download from link", callback_data="home:new"),
        InlineKeyboardButton(text="📋 My downloads", callback_data="queue:mine"),
    )
    builder.row(
        InlineKeyboardButton(text="⭐ Preferred formats", callback_data="favorites"),
        InlineKeyboardButton(text="⚙️ Settings", callback_data="settings"),
    )
    builder.row(InlineKeyboardButton(text="❓ Help", callback_data="home:help"))
    if is_admin:
        builder.row(InlineKeyboardButton(text="🛠 Admin Control Center", callback_data="admin:status"))
    return builder.as_markup()


def build_new_download_text(max_links: int) -> str:
    return (
        "📥 <b>Send a link</b>\n\n"
        "Send:\n• one media link\n"
        f"• up to {max_links} links in one message\n\n"
        "Separate multiple links with spaces or new lines.\n\n"
        "The bot will analyze them and let you choose the original source quality."
    )


def build_new_download_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ How downloads work", callback_data="help:quick")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home:show")],
    ])


HELP_SECTIONS = {"main", "quick", "quality", "batch", "queue", "settings", "failures", "privacy", "admin"}


def build_help_text(section: str = "main") -> str:
    texts = {
        "main": "❓ <b>Help</b>\n\nWhat do you need help with?",
        "quick": (
            "🚀 <b>Quick start</b>\n\n1. Send a media link.\n2. Wait for analysis.\n"
            "3. Choose the original source quality.\n4. Tap Download.\n"
            "5. The bot queues and sends the result automatically.\n\n"
            "For multiple links, paste them together in one message."
        ),
        "quality": (
            "🎞 <b>Choosing video quality</b>\n\n"
            "Preferred formats show your matching choices first. All Formats shows all source formats found.\n\n"
            "Codec is the source video encoding. Resolution is picture size. FPS is frame rate. "
            "Container is the file type.\n\nExact sizes have no prefix; estimated sizes begin with ~. "
            "Some qualities use separate source audio, which is combined without re-encoding."
        ),
        "batch": (
            "📦 <b>Multiple links and batches</b>\n\n"
            "✅ Ready — choose quality now\n🔎 Analyzing — analysis is running\n"
            "⏳ Waiting — it will start automatically\n🛡 Site blocked server — the website denied this server\n"
            "🔐 Sign-in required — anonymous access was refused\n⚠️ Failed — analysis could not finish\n\n"
            "Choose ready items opens selected links. Prepare ready downloads selects preferred qualities before queueing."
        ),
        "queue": (
            "📋 <b>Queue and progress</b>\n\nQueued items start automatically. A waiting item is saved until a safe slot opens.\n\n"
            "Progress moves through Downloading, Finalizing, Sending, and Complete. You do not need to resend the link."
        ),
        "settings": (
            "⚙️ <b>Settings</b>\n\nChoose Telegram video or file delivery, automatic or manual source audio, "
            "quality preference, preferred-format rules, and detailed or compact format information."
        ),
        "failures": (
            "⚠️ <b>Why a website may fail</b>\n\nA supported website may still block a cloud server, change its pages, or require sign-in.\n\n"
            "Private, paywalled, and DRM-protected media are not supported. The bot does not bypass access restrictions."
        ),
        "privacy": (
            "🛡 <b>Privacy and media handling</b>\n\nThe bot uses original source streams and does not transcode video. "
            "Temporary working files are cleaned according to bot policy.\n\n"
            "Operator-managed site sessions are never exposed in Telegram, and cookies cannot be uploaded through chat."
        ),
        "admin": (
            "🛠 <b>Admin help</b>\n\nUse the Admin Control Center for concurrency targets, resource safety, all downloads, "
            "users, site login sessions, maintenance, and system health. Core safety protections always remain active."
        ),
    }
    return texts.get(section, texts["main"])


def build_help_keyboard(section: str = "main", is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if section == "main":
        builder.row(
            InlineKeyboardButton(text="🚀 Quick start", callback_data="help:quick"),
            InlineKeyboardButton(text="🎞 Choosing video quality", callback_data="help:quality"),
        )
        builder.row(
            InlineKeyboardButton(text="📦 Multiple links / batches", callback_data="help:batch"),
            InlineKeyboardButton(text="📋 Queue & progress", callback_data="help:queue"),
        )
        builder.row(
            InlineKeyboardButton(text="⚙️ Settings", callback_data="help:settings"),
            InlineKeyboardButton(text="⚠️ Why a website may fail", callback_data="help:failures"),
        )
        builder.row(InlineKeyboardButton(text="🛡 Privacy & media handling", callback_data="help:privacy"))
        if is_admin:
            builder.row(InlineKeyboardButton(text="🛠 Admin help", callback_data="help:admin"))
        builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    else:
        builder.row(InlineKeyboardButton(text="↩️ Back to Help", callback_data="help:main"))
        builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def _job_quality(job: DownloadJob) -> str:
    value = (job.video_format_snapshot or {}).get("resolution_label")
    return str(value or ("Audio" if job.media_kind == "audio" else "Video"))


def build_queue_text(jobs: list[DownloadJob], positions: dict[str, int]) -> str:
    active_statuses = {
        JobStatus.CLAIMED, JobStatus.DOWNLOADING_VIDEO, JobStatus.DOWNLOADING_AUDIO,
        JobStatus.MERGING, JobStatus.UPLOADING,
    }
    active = [job for job in jobs if job.status in active_statuses]
    waiting = [job for job in jobs if job.status not in active_statuses]
    lines = ["📋 <b>My downloads</b>", "", "<b>Active</b>"]
    if not active:
        lines.append("No active downloads.")
    for index, job in enumerate(active, 1):
        quality = _job_quality(job)
        stage = {
            JobStatus.CLAIMED: "Preparing", JobStatus.DOWNLOADING_VIDEO: "Downloading",
            JobStatus.DOWNLOADING_AUDIO: "Downloading audio", JobStatus.MERGING: "Finalizing",
            JobStatus.UPLOADING: "Sending to Telegram",
        }.get(job.status, "Active")
        suffix = (
            f" • {job.progress_pct:.0f}% current stage"
            if job.status in {JobStatus.DOWNLOADING_VIDEO, JobStatus.DOWNLOADING_AUDIO}
            else ""
        )
        lines.append(f"{index}. {escape(quality)}\n   {escape(stage)}{suffix}")
    lines.extend(("", "<b>Waiting</b>"))
    if not waiting:
        lines.append("No waiting downloads.")
    for index, job in enumerate(waiting, len(active) + 1):
        quality = _job_quality(job)
        position = positions.get(job.job_id)
        pos = f"Position {position}" if position else "Waiting"
        lines.append(f"{index}. {escape(quality)}\n   {pos} · Starts automatically")
    return "\n".join(lines)


def build_queue_keyboard(jobs: list[DownloadJob], *, admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔄 Refresh", callback_data="queue:mine"))
    for index, job in enumerate(jobs[:8], 1):
        builder.row(InlineKeyboardButton(
            text=f"❌ Cancel #{index} · {_job_quality(job)}", callback_data=f"qcancel:{job.job_id}"
        ))
    if admin:
        builder.row(InlineKeyboardButton(text="🌐 All downloads", callback_data="ops:queue"))
    builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def _size_display(size: int | None, exact: bool) -> str:
    if size is None:
        return "Unknown size"
    mb = size / 1024**2
    if mb >= 1024:
        value = f"{mb / 1024:.2f} GB"
    else:
        value = f"{mb:.1f} MB"
    return value if exact else f"~{value}"


def _bitrate_display(value: float | None) -> str:
    if not value:
        return "Unknown"
    return f"~{value / 1000:.1f} Mbps" if value >= 1000 else f"~{value:g} kbps"


def _filter_value(filters: dict[str, object], name: str) -> str:
    value = filters.get(name) or ["any"]
    values = value if isinstance(value, list) else [value]
    return ", ".join(str(item).title() for item in values)


def format_button_label(fmt: MediaFormat, audio: MediaFormat | None = None) -> str:
    codec = fmt.vcodec_normalized.value.replace("H.265 / HEVC", "H265").replace("H.264 / AVC", "H264")
    quality = fmt.resolution_label or (f"{fmt.height}p" if fmt.height else "Unknown")
    fps = f" · {fmt.fps:g} FPS" if fmt.fps else ""
    total, exact = calculate_total_download_size(fmt, audio)
    return f"{codec} · {quality}{fps} · {_size_display(total, exact)}"


def stream_status_text(fmt: MediaFormat) -> str:
    if fmt.is_muxed:
        return "Video and audio are already included in this source format."
    if not fmt.requires_separate_audio:
        return "This original source stream needs no additional processing."
    return (
        "Separate source audio will be combined losslessly without video re-encoding."
    )


def build_preferred_media_text(
    session: MediaSession, result: FavoriteMatchResult, page: int = 0,
    page_size: int = 6, *, automatic_audio: bool = True,
) -> str:
    pages = max(1, math.ceil(len(result.formats) / page_size))
    page = max(0, min(page, pages - 1))
    visible_formats = result.formats[page * page_size:(page + 1) * page_size]
    videos = sum(1 for fmt in session.formats if fmt.is_video)
    lines = [
        "🎬 <b>Video ready</b>", "",
        f"<b>{escape(_short(session.title))}</b>",
        f"{escape(_source_label(session.canonical_url or session.url))}",
        f"Duration: {_duration(session.duration)}", "",
        f"⭐ <b>Preferred formats</b> · {videos} source qualities",
    ]
    if not result.formats:
        lines.append("No enabled favorite rule has an exact match in this source.")
    for fmt in visible_formats:
        audio = select_default_audio(session.formats, fmt) if automatic_audio else None
        lines.extend(("", f"⭐ <b>{escape(format_button_label(fmt, audio))}</b>", stream_status_text(fmt)))
    if result.total_combinations:
        available = result.total_combinations - result.unavailable_combinations
        lines.extend(("", f"{available} of {result.total_combinations} preferred combinations have an exact match."))
    if pages > 1:
        lines.extend(("", f"Page {page + 1} of {pages}"))
    lines.extend(("", "Original source quality · No compression or video re-encoding"))
    return "\n".join(lines)


def build_preferred_keyboard(
    session: MediaSession, result: FavoriteMatchResult, page: int = 0,
    page_size: int = 6, *, automatic_audio: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    pages = max(1, math.ceil(len(result.formats) / page_size))
    page = max(0, min(page, pages - 1))
    for fmt in result.formats[page * page_size:(page + 1) * page_size]:
        audio = select_default_audio(session.formats, fmt) if automatic_audio else None
        builder.row(InlineKeyboardButton(
            text="⭐ " + format_button_label(fmt, audio), callback_data=f"detail:{session.session_id}:{fmt.internal_key}"
        ))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀ Previous", callback_data=f"preferred:{session.session_id}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                text="Next ▶", callback_data=f"preferred:{session.session_id}:{page + 1}"
            ))
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🎞 View all qualities", callback_data=f"all:{session.session_id}:0"))
    builder.row(
        InlineKeyboardButton(text="🎵 Download audio only", callback_data=f"audio:{session.session_id}"),
    )
    if session.thumbnails:
        builder.row(InlineKeyboardButton(text="🖼 Download thumbnail", callback_data=f"assets:{session.session_id}:thumb"))
    builder.row(
        InlineKeyboardButton(text="ℹ️ Media details", callback_data=f"info:{session.session_id}"),
        InlineKeyboardButton(text="⚙️ Download settings", callback_data=f"settings:{session.session_id}"),
    )
    if session.subtitles:
        builder.row(InlineKeyboardButton(text="💬 Subtitles", callback_data=f"assets:{session.session_id}:sub"))
    builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def _duration(value: float | None) -> str:
    seconds_value = whole_duration_seconds(value)
    if seconds_value is None:
        return "Unknown"
    hours, remainder = divmod(seconds_value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def build_format_details_text(
    fmt: MediaFormat, audio: MediaFormat | None, style: str = "rich",
    *, manual_audio_required: bool = False,
    telegram_capabilities: TelegramCapabilities | None = None,
) -> str:
    if style == "compact":
        audio_note = (
            "\n\n🎵 Manual audio selection required. Expected download depends on selected audio."
            if manual_audio_required else ""
        )
        return (
            f"🎞 <b>Selected quality</b>\n\n"
            f"{escape(format_button_label(fmt, audio if not manual_audio_required else None))}\n"
            f"{stream_status_text(fmt)}{audio_note}\n\nNo compression or re-encoding will occur."
        )
    total = fmt.effective_size
    exact = fmt.is_exact_size
    if audio:
        total = total + audio.effective_size if total is not None and audio.effective_size is not None else None
        exact = exact and audio.is_exact_size
    total_text = _size_display(total, exact)
    audio_text = "Manual audio selection required" if manual_audio_required else ("Already included" if fmt.is_muxed else (
        f"{audio.audio_language or 'Unknown language'} · {audio.acodec_normalized.value} · "
        f"{audio.abr:g} kbps" if audio and audio.abr else
        (f"{audio.audio_language or 'Unknown language'} · {audio.acodec_normalized.value}" if audio else "Unavailable")
    ))
    expected_text = "Depends on selected audio" if manual_audio_required else total_text
    audio_size_text = (
        "Choose an exact stream" if manual_audio_required else
        (audio.format_size_display() if audio else ('Included' if fmt.is_muxed else 'Unknown size'))
    )
    dimensions = f"{fmt.width}×{fmt.height}" if fmt.width and fmt.height else "Unknown dimensions"
    fps_text = f"{fmt.fps:g} FPS" if fmt.fps is not None else "Unknown FPS"
    capability_note = ""
    if (
        telegram_capabilities
        and telegram_capabilities.local_api_enabled
        and telegram_capabilities.endpoint_available
        and total is not None
        and total > 50 * 1024**2
        and total <= telegram_capabilities.max_upload_bytes
    ):
        capability_note = "\n✅ Large-file delivery available"
    return (
        "🎞 <b>Selected quality</b>\n\n"
        f"{dimensions}\n"
        f"{escape(fmt.vcodec_normalized.value)} · {escape(fmt.ext.upper())}"
        f"{' · ' + fps_text if fmt.fps is not None else ''}\n"
        f"Protocol: {escape(fmt.protocol or 'Unknown')}\n"
        f"Bitrate: {_bitrate_display(fmt.vbr or fmt.tbr)}\n"
        f"Source format ID: <code>{escape(fmt.format_id)}</code>\n\n"
        f"🎵 <b>Audio</b>\n{escape(audio_text)}\n\n"
        f"📦 <b>Estimated download</b>\nVideo: {fmt.format_size_display()}\n"
        f"Audio: {audio_size_text}\n"
        f"Total: {expected_text}{capability_note}\n\n🔧 {stream_status_text(fmt)}\n\n"
        "Original source streams are preserved."
    )


def build_format_details_keyboard(
    session: MediaSession, fmt: MediaFormat, *, back: str = "preferred",
    manual_audio_required: bool = False, back_page: int = 0,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬇️ Download this quality", callback_data=f"fmt:{session.session_id}:{fmt.internal_key}"))
    if fmt.requires_separate_audio:
        builder.row(InlineKeyboardButton(
            text="🎵 Choose audio" if manual_audio_required else "🎵 Change audio",
            callback_data=f"audopts:{session.session_id}:{fmt.internal_key}",
        ))
    back_data = (
        f"preferred:{session.session_id}" if back == "preferred"
        else f"all:{session.session_id}:{max(0, back_page)}"
    )
    builder.row(InlineKeyboardButton(
        text="↩️ Back to recommended" if back == "preferred" else "↩️ Back to formats",
        callback_data=back_data,
    ))
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
            text="🎞 " + format_button_label(fmt),
            callback_data=f"adetail:{session.session_id}:{fmt.internal_key}:{page}",
        ))
    builder.row(
        InlineKeyboardButton(text=f"🎞 Codec: {_filter_value(filters, 'codec')}", callback_data=f"filter:{session.session_id}:codec"),
        InlineKeyboardButton(text=f"📐 Resolution: {_filter_value(filters, 'resolution')}", callback_data=f"filter:{session.session_id}:resolution"),
    )
    builder.row(
        InlineKeyboardButton(text=f"🎬 FPS: {_filter_value(filters, 'fps')}", callback_data=f"filter:{session.session_id}:fps"),
        InlineKeyboardButton(text=f"📦 Container: {_filter_value(filters, 'container')}", callback_data=f"filter:{session.session_id}:container"),
    )
    builder.row(InlineKeyboardButton(text="🔎 Search formats", callback_data=f"search:{session.session_id}"))
    builder.row(InlineKeyboardButton(text="♻️ Clear filters", callback_data=f"freset:{session.session_id}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Previous", callback_data=f"all:{session.session_id}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"Page {page+1} of {pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"all:{session.session_id}:{page+1}"))
    builder.row(*nav)
    builder.row(InlineKeyboardButton(text="↩️ Recommended formats", callback_data=f"preferred:{session.session_id}"))
    return builder.as_markup()


def build_all_formats_text(
    session: MediaSession, filters: dict[str, object], page: int = 0,
    page_size: int = 6,
) -> str:
    values = filtered_video_formats(session, filters)
    pages = max(1, math.ceil(len(values) / page_size))
    page = max(0, min(page, pages - 1))
    video_count = sum(1 for fmt in session.formats if fmt.is_video)
    audio_count = sum(1 for fmt in session.formats if fmt.is_audio and not fmt.is_video)

    def selected(name: str) -> str:
        value = filters.get(name) or ["any"]
        if isinstance(value, list):
            return ", ".join(str(item) for item in value).title()
        return str(value)

    lines = [
        "🔍 <b>All source qualities</b> · all source formats found", "",
        f"Page {page + 1} / {pages}",
        f"Showing {len(values)} of {video_count} video formats", "",
        "<b>Filters</b>",
        f"Codec: {escape(selected('codec'))}",
        f"Resolution: {escape(selected('resolution'))}",
        f"Frame rate: {escape(selected('fps'))}",
        f"Container: {escape(selected('container'))}",
    ]
    query = str(filters.get("query") or "").strip()
    if query:
        lines.append(f"Search: {escape(query)}")
    if audio_count:
        lines.extend(("", f"{audio_count} source audio track{'s' if audio_count != 1 else ''} available."))
    return "\n".join(lines)


def build_filter_keyboard(session_id: str, category: str, selected: list[str]) -> InlineKeyboardMarkup:
    choices = {
        "codec": ["any", "h265", "h264", "vp9", "av1", "other"],
        "resolution": ["any", "2160", "1440", "1080", "720", "480", "360", "other"],
        "fps": ["any", "best", "60", "30", "24", "other"],
        "container": ["any", "mp4", "webm", "mkv-compatible", "other"],
    }[category]
    builder = InlineKeyboardBuilder()
    category_token = {
        "codec": "c", "resolution": "r", "fps": "f", "container": "e",
    }[category]
    for value in choices:
        mark = "☑" if value in selected else "☐"
        builder.button(
            text=f"{mark} {value}", callback_data=f"ft:{session_id}:{category_token}:{value}"
        )
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="✅ Apply", callback_data=f"all:{session_id}:0"))
    builder.row(InlineKeyboardButton(text="↩️ Back to formats", callback_data=f"all:{session_id}:0"))
    return builder.as_markup()


def build_search_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Clear search", callback_data=f"searchclear:{session_id}")],
        [InlineKeyboardButton(text="↩️ Back to formats", callback_data=f"all:{session_id}:0")],
    ])


def build_audio_keyboard(session: MediaSession, video_key: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    values = sorted(
        (item for item in session.formats if item.is_audio and not item.is_video),
        key=lambda item: (-(item.abr or item.tbr or 0), item.format_id),
    )
    video = session.get_format_by_key(video_key) if video_key else None
    recommended = select_default_audio(session.formats, video) if video else None
    for fmt in values:
        bitrate = fmt.abr or fmt.tbr
        prefix = "⭐ " if recommended and recommended.internal_key == fmt.internal_key else "🎵 "
        label = (
            f"{prefix}{fmt.audio_language or 'Unknown language'} · {fmt.acodec_normalized.value}"
            f"{' · ' + f'{bitrate:g} kbps' if bitrate else ''} · {fmt.format_size_display()}"
        )
        callback = (
            f"fmta:{session.session_id}:{video_key}:{fmt.internal_key}"
            if video_key else f"aonly:{session.session_id}:{fmt.internal_key}"
        )
        builder.row(InlineKeyboardButton(text=label, callback_data=callback))
    back = f"detail:{session.session_id}:{video_key}" if video_key else f"preferred:{session.session_id}"
    builder.row(InlineKeyboardButton(
        text="↩️ Back to video quality" if video_key else "↩️ Back to video",
        callback_data=back,
    ))
    return builder.as_markup()


def build_audio_text(session: MediaSession, video_key: str | None = None) -> str:
    video = session.get_format_by_key(video_key) if video_key else None
    recommended = select_default_audio(session.formats, video) if video else None
    if video:
        recommendation = ""
        if recommended:
            bitrate = recommended.abr or recommended.tbr
            recommendation = (
                "\n\n<b>Recommended</b>\n⭐ "
                f"{escape(recommended.audio_language or 'Unknown language')} · "
                f"{escape(recommended.acodec_normalized.value)}"
                f"{' · ' + f'{bitrate:g} kbps' if bitrate else ''}"
            )
        return (
            "🎵 <b>Choose audio track</b>\n\n"
            "This video quality contains no audio. Choose an original source audio stream to combine with it."
            f"{recommendation}\n\nNo audio conversion will occur."
        )
    return (
        "🎵 <b>Audio only</b>\n\nChoose an original source audio track to download.\n\n"
        "No MP3 conversion or audio re-encoding will occur."
    )


def build_settings_text(settings: UserSettings) -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"📤 Send downloads as:\n<b>{'Telegram Video' if settings.send_mode == 'video' else 'File / Document'}</b>\n\n"
        f"⭐ Quality preference:\n<b>{_strategy_label(settings.matching_strategy.value)}</b>\n\n"
        f"🎵 Audio handling:\n<b>{'Automatic' if settings.automatic_audio else 'Choose manually'}</b>\n\n"
        f"🧾 Format information:\n<b>{'Detailed' if settings.detail_style.value == 'rich' else 'Compact'}</b>"
    )


def build_settings_keyboard(
    settings: UserSettings, media_session_id: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📤 Delivery type", callback_data="uset:delivery" + (f":{media_session_id}" if media_session_id else "")
        ),
        InlineKeyboardButton(text="⭐ Preferred formats", callback_data="favorites"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🎵 Audio handling", callback_data="uset:audio" + (f":{media_session_id}" if media_session_id else "")
        ),
        InlineKeyboardButton(
            text="🎯 Quality preference", callback_data="uset:strategy" + (f":{media_session_id}" if media_session_id else "")
        ),
    )
    builder.row(InlineKeyboardButton(
        text="🧾 Display style", callback_data="uset:display" + (f":{media_session_id}" if media_session_id else "")
    ))
    if media_session_id:
        builder.row(InlineKeyboardButton(
            text="↩️ Back to video", callback_data=f"preferred:{media_session_id}:0"
        ))
    else:
        builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def build_settings_section_keyboard(
    settings: UserSettings, section: str, media_session_id: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    def setting_callback(category: str, value: str) -> str:
        base = f"set:{category}:{value}"
        return f"{base}:{media_session_id}" if media_session_id else base

    if section == "delivery":
        builder.row(
            InlineKeyboardButton(
                text=("● Telegram Video" if settings.send_mode == "video" else "○ Telegram Video"),
                callback_data=setting_callback("send", "video"),
            ),
            InlineKeyboardButton(
                text=("● File / Document" if settings.send_mode == "document" else "○ File / Document"),
                callback_data=setting_callback("send", "document"),
            ),
        )
    elif section == "audio":
        builder.row(InlineKeyboardButton(
            text=("● Automatic audio" if settings.automatic_audio else "○ Automatic audio"),
            callback_data=setting_callback("auto_audio", "toggle"),
        ))
        builder.row(InlineKeyboardButton(
            text=("● Choose audio manually" if not settings.automatic_audio else "○ Choose audio manually"),
            callback_data=setting_callback("auto_audio", "toggle"),
        ))
    elif section in {"strategy", "workflow"}:
        for value, label in (
            ("best_quality", "Best quality"), ("smallest_file", "Smallest file"),
            ("prefer_ready", "Prefer ready-made video"),
        ):
            builder.row(InlineKeyboardButton(
                text=("● " if settings.matching_strategy.value == value else "○ ") + label,
                callback_data=setting_callback("strategy", value),
            ))
    else:
        builder.row(
            InlineKeyboardButton(
                text=("● Detailed" if settings.detail_style.value == "rich" else "○ Detailed"),
                callback_data=setting_callback("detail", "rich"),
            ),
            InlineKeyboardButton(
                text=("● Compact" if settings.detail_style.value == "compact" else "○ Compact"),
                callback_data=setting_callback("detail", "compact"),
            ),
        )
    back = "settings" + (f":{media_session_id}" if media_session_id else "")
    builder.row(InlineKeyboardButton(text="↩️ Back to settings", callback_data=back))
    return builder.as_markup()


def _strategy_label(value: str) -> str:
    return {
        "best_quality": "Best quality",
        "smallest_file": "Smallest file",
        "prefer_ready": "Prefer ready-made video",
    }.get(value, value.replace("_", " ").title())


def build_favorites_text(rules: list[FavoriteFormatRule]) -> str:
    lines = [
        "⭐ <b>Preferred Format Rules</b>", "",
        "These rules decide which qualities appear first after a link is analyzed.",
        "They never convert the video.",
    ]
    for index, rule in enumerate(rules, 1):
        state = "✅" if rule.enabled else "⏸"
        lines.extend(("", f"{index}. {state} <b>{escape(rule.name)}</b>",
                      f"Codec: {escape(_choices_text(rule.codecs))}",
                      f"Resolution: {escape(_choices_text(rule.resolutions, suffix='p'))}",
                      f"Frame rate: {escape(_choices_text(rule.fps_values))}",
                      f"Container: {escape(_choices_text(rule.containers))}"))
    return "\n".join(lines)


def build_favorites_keyboard(
    rules: list[FavoriteFormatRule], media_session_id: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for rule in rules:
        builder.row(InlineKeyboardButton(
            text=f"✏️ Edit {_short(rule.name, 32)}", callback_data=f"fedit:{rule.rule_id}"
        ))
    builder.row(InlineKeyboardButton(text="➕ Add preferred rule", callback_data="fadd"))
    settings_callback = f"settings:{media_session_id}" if media_session_id else "settings"
    builder.row(InlineKeyboardButton(text="↩️ Back to settings", callback_data=settings_callback))
    return builder.as_markup()


def build_rule_editor(rule: FavoriteFormatRule) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category, label in (
        ("codec", "🎞 Codec"), ("resolution", "📐 Resolution"),
        ("fps", "🎬 Frame rate"), ("container", "📦 Container"),
    ):
        builder.row(InlineKeyboardButton(text=label, callback_data=f"frcat:{rule.rule_id}:{category}"))
    builder.row(
        InlineKeyboardButton(text="📋 Duplicate rule", callback_data=f"frop:{rule.rule_id}:dup"),
        InlineKeyboardButton(
            text="⏸ Disable rule" if rule.enabled else "✅ Enable rule",
            callback_data=f"frop:{rule.rule_id}:toggle",
        ),
    )
    builder.row(
        InlineKeyboardButton(text="⬆ Move up", callback_data=f"frop:{rule.rule_id}:up"),
        InlineKeyboardButton(text="⬇ Move down", callback_data=f"frop:{rule.rule_id}:down"),
    )
    builder.row(InlineKeyboardButton(text="🗑 Delete rule", callback_data=f"fdelete:{rule.rule_id}"))
    builder.row(InlineKeyboardButton(text="↩️ Preferred rules", callback_data="favorites"))
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
    builder.row(InlineKeyboardButton(text="↩️ Back to rule", callback_data=f"fedit:{rule.rule_id}"))
    return builder.as_markup()


def build_rule_delete_keyboard(rule: FavoriteFormatRule) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Delete rule", callback_data=f"fdeleteconfirm:{rule.rule_id}")],
        [InlineKeyboardButton(text="↩️ Keep rule", callback_data=f"fedit:{rule.rule_id}")],
    ])


def _choices_text(values: list[str], suffix: str = "") -> str:
    if "any" in values:
        return "Any"
    return ", ".join(
        (value + suffix if suffix and value.isdigit() else value.upper() if value in {"h264", "h265", "vp9", "av1"} else value.title())
        for value in values
    )


def build_assets_keyboard(session: MediaSession, kind: str) -> InlineKeyboardMarkup:
    values = session.thumbnails if kind == "thumb" else session.subtitles
    builder = InlineKeyboardBuilder()
    for asset in values[:20]:
        if kind == "thumb":
            label = f"{asset.width or '?'}×{asset.height or '?'} · {asset.ext.upper()}"
        else:
            source = "Auto-generated" if asset.autogenerated else "Creator"
            label = f"{asset.language or 'Unknown'} · {source} · {asset.ext.upper()}"
        builder.row(InlineKeyboardButton(
            text="⬇️ " + label, callback_data=f"asset:{session.session_id}:{asset.asset_id}"
        ))
    builder.row(InlineKeyboardButton(text="↩️ Back to video", callback_data=f"preferred:{session.session_id}"))
    return builder.as_markup()


def batch_item_status_text(item: MediaItem) -> str:
    if item.analysis_status == "ready":
        return "✅ Ready"
    if item.analysis_status == "analyzing":
        return "🔎 Analyzing"
    if item.analysis_status == "waiting":
        return "⏳ Waiting for analysis"
    if item.analysis_status != "failed":
        return "⏳ Waiting"
    return {
        "authentication_required": "🔐 Sign-in required",
        "site_access_challenge": "🛡 Site blocked this server",
        "extraction_timeout": "⏳ Analysis timed out",
        "unsupported_url": "❌ Unsupported link",
        "media_unavailable": "❌ Media unavailable",
        "access_revoked": "⛔ Access revoked",
        "extraction_failed": "❌ Analysis error",
        "unexpected_error": "❌ Analysis error",
    }.get(item.analysis_error_category, "❌ Failed")


def build_collection_text(session: MediaSession) -> str:
    title = (
        "🔗 <b>Batch URLs</b>" if session.session_kind == "batch" else
        "📚 <b>Multi-media Post</b>" if session.session_kind == "multimedia" else
        "📚 <b>Playlist Detected</b>"
    )
    if session.session_kind == "batch":
        ready = sum(item.analysis_status == "ready" for item in session.items)
        analyzing = sum(item.analysis_status == "analyzing" for item in session.items)
        waiting = sum(item.analysis_status == "waiting" for item in session.items)
        attention = len(session.items) - ready - analyzing - waiting
        summary = f"{len(session.items)} links · {ready} ready"
        if analyzing:
            summary += f" · {analyzing} analyzing"
        if waiting:
            summary += f" · {waiting} waiting"
        if attention:
            summary += f" · {attention} need attention"
        lines = ["📦 <b>Batch</b>", "", summary, ""]
        for index, item in enumerate(session.items, 1):
            source = _source_label(item.source_url)
            state = batch_item_status_text(item)
            lines.append(f"{index}. {state.split(' ', 1)[0]} <b>{escape(source)}</b>")
            if item.analysis_status == "ready":
                lines.append(f"   {escape(_short(item.title, 64, source))}")
            else:
                lines.append(f"   {escape(state.split(' ', 1)[-1])}")
            lines.append("")
        if ready:
            lines.extend((
                "<b>Next step</b>",
                "Open a ready item to choose its quality, or prepare all ready downloads.",
            ))
        elif analyzing or waiting:
            lines.extend(("<b>Next step</b>", "Analysis continues automatically. No resend is needed."))
        else:
            lines.extend(("<b>Next step</b>", "Open an item for details or return Home."))
        return "\n".join(lines)
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
        if session.session_kind == "batch":
            source = _source_label(item.source_url)
            if item.analysis_status == "ready":
                label = f"✅ {index} · {source} · Choose quality"
            elif item.analysis_status == "analyzing":
                label = f"🔎 {index} · {source} · Analyzing"
            elif item.analysis_status == "waiting":
                label = f"⏳ {index} · {source} · Waiting"
            elif item.analysis_error_category == "site_access_challenge":
                label = f"🛡 {index} · {source} · Details"
            elif item.analysis_error_category == "authentication_required":
                label = f"🔐 {index} · {source} · Details"
            else:
                label = f"⚠️ {index} · {source} · Details"
        else:
            icon = "🖼" if item.kind == "image" else "🎞"
            label = f"{icon} Open item {index}"
        builder.row(InlineKeyboardButton(
            text=label, callback_data=f"item:{session.session_id}:{item.item_id}"
        ))
    pages = max(1, math.ceil(len(session.items) / page_size))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Previous", callback_data=f"collection:{session.session_id}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"Page {page+1} of {pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"collection:{session.session_id}:{page+1}"))
    builder.row(*nav)
    if session.session_kind == "playlist":
        builder.row(
            InlineKeyboardButton(text="☑️ Choose first 5", callback_data=f"items:{session.session_id}:5"),
            InlineKeyboardButton(text="☑️ Choose first 10", callback_data=f"items:{session.session_id}:10"),
        )
    builder.row(InlineKeyboardButton(
        text=("☑️ Choose ready items" if any(item.analysis_status == "ready" for item in session.items)
              else "📋 View all items") if session.session_kind == "batch" else "☑️ Choose items",
        callback_data=f"iselect:{session.session_id}",
    ))
    if session.session_kind == "batch" and any(item.analysis_status == "ready" for item in session.items):
        builder.row(InlineKeyboardButton(
            text=f"⬇️ Prepare {sum(item.analysis_status == 'ready' for item in session.items)} ready downloads",
            callback_data=f"allmedia:{session.session_id}"
        ))
    if session.session_kind == "multimedia":
        builder.row(InlineKeyboardButton(
            text="⬇️ Prepare all media", callback_data=f"allmedia:{session.session_id}"
        ))
    active = any(item.analysis_status in {"waiting", "analyzing"} for item in session.items)
    if session.session_kind == "batch" and active:
        builder.row(InlineKeyboardButton(
            text="⏹ Stop batch analysis", callback_data=f"cancel_session:{session.session_id}"
        ))
    else:
        builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def build_batch_item_detail_text(item: MediaItem) -> str:
    source = escape(_source_label(item.source_url))
    if item.analysis_status == "waiting":
        return (
            f"⏳ <b>{source} is waiting</b>\n\nAnother link is currently being analyzed. "
            "It will start automatically.\n\nYou do not need to resend it."
        )
    if item.analysis_status == "analyzing":
        return (
            f"🔎 <b>{source} is being analyzed</b>\n\nThe bot is checking available source qualities.\n\n"
            "You do not need to resend it."
        )
    if item.analysis_error_category == "site_access_challenge":
        return (
            f"🛡 <b>{source} could not be analyzed</b>\n\n{source} refused the request from this server.\n\n"
            "Your link may still be valid. This is a website/server-access restriction, not a video-format problem.\n\n"
            "Try again later, or use an operator-authorized site session if one is configured."
        )
    if item.analysis_error_category == "authentication_required":
        return (
            f"🔐 <b>{source} sign-in required</b>\n\n{source} is requiring an authenticated browser session from this server.\n\n"
            "The bot cannot bypass this restriction. An operator-authorized session can be configured separately."
        )
    return build_error_text(item.analysis_error_category)


def build_batch_item_detail_keyboard(session: MediaSession, item: MediaItem) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if item.analysis_status == "failed" and item.analysis_error_category != "authentication_required":
        rows.append([InlineKeyboardButton(
            text="🔄 Try analysis again", callback_data=f"batchretry:{session.session_id}:{item.item_id}"
        )])
    if item.analysis_status == "failed":
        rows.append([InlineKeyboardButton(
            text="❓ Why can websites fail?", callback_data="help:failures"
        )])
    rows.append([InlineKeyboardButton(
        text="↩️ Back to batch", callback_data=f"collection:{session.session_id}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_analysis_error_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Why can websites fail?", callback_data="help:failures")],
        [InlineKeyboardButton(text="🏠 Home", callback_data="home:show")],
    ])


def build_batch_review_text(session: MediaSession, selected: set[str]) -> str:
    ready = sum(item.analysis_status == "ready" for item in session.items)
    selected_ready = sum(
        item.analysis_status == "ready" and item.item_id in selected for item in session.items
    )
    lines = [
        "☑️ <b>Choose ready items</b>", "",
        "Select which ready links you want to download.", "",
    ]
    for index, item in enumerate(session.items, 1):
        source = escape(_source_label(item.source_url))
        if item.analysis_status == "ready":
            lines.append(f"{index}. ✅ {source}")
        else:
            lines.append(f"{index}. {batch_item_status_text(item)} — unavailable")
    lines.extend(("", f"Selected: {selected_ready} of {ready} available"))
    return "\n".join(lines)


def build_batch_review_keyboard(session: MediaSession, selected: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, item in enumerate(session.items, 1):
        source = _source_label(item.source_url)
        if item.analysis_status == "ready":
            mark = "☑" if item.item_id in selected else "☐"
            callback_data = f"itoggle:{session.session_id}:{item.item_id}:0"
        else:
            mark = "🔒"
            callback_data = f"item:{session.session_id}:{item.item_id}"
        builder.row(InlineKeyboardButton(
            text=f"{mark} {index} · {source}", callback_data=callback_data
        ))
    count = sum(
        item.analysis_status == "ready" and item.item_id in selected for item in session.items
    )
    builder.row(InlineKeyboardButton(
        text=f"⬇️ Continue with {count} selected",
        callback_data=f"iprocess:{session.session_id}",
    ))
    builder.row(InlineKeyboardButton(text="🔄 Refresh", callback_data=f"iselect:{session.session_id}:refresh"))
    builder.row(InlineKeyboardButton(text="↩️ Back to batch", callback_data=f"iselect:{session.session_id}:back"))
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
        builder.row(InlineKeyboardButton(
            text=f"{mark} {index} · {_short(item.title, 34)}",
            callback_data=f"itoggle:{session.session_id}:{item.item_id}:{page}"
        ))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                text="◀ Previous", callback_data=f"iselect:{session.session_id}:{page - 1}"
            ))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(
                text="Next ▶", callback_data=f"iselect:{session.session_id}:{page + 1}"
            ))
        builder.row(*nav)
    builder.row(InlineKeyboardButton(
        text=f"⬇️ Continue with {len(selected)} selected", callback_data=f"iprocess:{session.session_id}"
    ))
    builder.row(InlineKeyboardButton(text="↩️ Back to collection", callback_data=f"collection:{session.session_id}"))
    return builder.as_markup()


def build_media_info_details(session: MediaSession) -> str:
    videos = sum(1 for fmt in session.formats if fmt.is_video)
    audio = sum(1 for fmt in session.formats if fmt.is_audio and not fmt.is_video)
    description = (session.description or "Unknown").replace("\n", " ")[:500]
    return (
        "ℹ️ <b>Media details</b>\n\n"
        f"<b>Title:</b> {escape(_short(session.title, 120))}\n"
        f"<b>Creator:</b> {escape(session.uploader or 'Unknown')}\n"
        f"<b>Source:</b> {escape(_source_label(session.canonical_url or session.url))}\n"
        f"<b>Duration:</b> {_duration(session.duration)}\n"
        f"<b>Upload date:</b> {escape(session.upload_date or 'Unknown')}\n"
        f"<b>Canonical URL:</b> {escape(_safe_display_url(session.canonical_url or session.url))}\n"
        f"<b>Formats:</b> {videos} video, {audio} audio\n\n"
        f"<b>Description:</b> {escape(description)}"
    )


def _safe_display_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def build_admin_status_text(
    sample: dict, stats: dict[str, int], limits: dict[str, int], mode: str, paused: bool,
    disk_free: int, telegram_capabilities: TelegramCapabilities | None = None,
    extractor_diagnostics: tuple[str, int] | None = None,
) -> str:
    cpu = sample["cpu"]
    memory = sample["memory"]
    delivery = ""
    if telegram_capabilities:
        transport, limit = telegram_capabilities.status_lines()
        delivery = f"\n\n📤 <b>Telegram delivery</b>\n\n{escape(transport)}\n{escape(limit)}"
    extractor = ""
    if extractor_diagnostics:
        version, override_count = extractor_diagnostics
        extractor = (
            "\n\n🧩 <b>Extractor engine</b>\n\n"
            f"yt-dlp {escape(version)}\nCompatibility overrides: {override_count} enabled\n"
            "Generic extractor: available"
        )
    return (
        "🛠 <b>Admin Control Center</b>\n\n"
        f"CPU: {float(cpu['load_percent']):.0f}%\n"
        f"Memory available: {int(memory['available']) / 1024**3:.2f} GB\n"
        f"Disk free: {disk_free / 1024**3:.2f} GB\n\n"
        "📥 <b>Queue</b>\n\n"
        f"Active: {stats['active']}\nWaiting: {stats['waiting']}\nFailed: {stats['failed']}\n\n"
        "🛡 <b>Resource safety</b>\n\n"
        f"Mode: {escape(mode)}\nNew downloads: {'Paused' if paused else 'Accepted'}\n"
        f"Allowed now — analysis {limits['extraction']}, downloads {limits['download']}, "
        f"finalizing {limits['merge']}, Telegram sends {limits['upload']}\n\n"
        "🤖 <b>Bot</b>\n\n"
        f"Successful jobs: {stats['successful']}\nFailures: {stats['failed']}\n"
        f"Cache hits: {stats['cache_hits']}"
        f"{delivery}{extractor}"
    )


def build_delivery_limit_text(
    capabilities: TelegramCapabilities, size_bytes: int | None, *, estimated: bool = True,
) -> str:
    size = _size_display(size_bytes, not estimated) if size_bytes is not None else "Unknown"
    if capabilities.local_api_enabled and not capabilities.endpoint_available:
        return (
            "📦 <b>Large-file Telegram delivery is temporarily unavailable</b>\n\n"
            f"Estimated size: {size}\n"
            "The configured Local Bot API endpoint is not ready. No cloud upload was attempted."
        )
    title = "This quality is too large to send" if estimated else "This file is too large to send"
    label = "Estimated size" if estimated else "File size"
    return (
        f"📦 <b>{title}</b>\n\n"
        f"{label}: {size}\nCurrent Telegram limit: {capabilities.max_upload_bytes // 1024**2} MB\n\n"
        "Choose another original source quality. No compression fallback is used."
    )


def build_delivery_limit_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎞 Choose another quality", callback_data=f"preferred:{session_id}",
        )],
        [InlineKeyboardButton(text="↩️ Back", callback_data=f"preferred:{session_id}")],
    ])


def build_admin_keyboard(paused: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚡ Concurrency & limits", callback_data="admin:performance"),
        InlineKeyboardButton(text="📋 All downloads", callback_data="ops:queue"),
    )
    builder.row(
        InlineKeyboardButton(text="🛡 Resource safety mode", callback_data="admin:resources"),
        InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
    )
    builder.row(
        InlineKeyboardButton(text="🔐 Site login sessions", callback_data="ops:sessions"),
        InlineKeyboardButton(text="🧹 Maintenance", callback_data="admin:maintenance"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 System health", callback_data="ops:status"),
    )
    builder.row(InlineKeyboardButton(
        text="▶️ Accept new downloads" if paused else "⏸ Pause new downloads",
        callback_data="admin:resume" if paused else "admin:pause",
    ))
    builder.row(InlineKeyboardButton(text="🏠 Home", callback_data="home:show"))
    return builder.as_markup()


def build_performance_text(
    values: dict[str, object], effective: dict[str, int], active: dict[str, int],
    reason: str,
) -> str:
    return (
        "⚡ <b>Concurrency & limits</b>\n\n"
        "Target is the configured maximum. Allowed now may be lower when resource safety is active. "
        "Running shows work currently in progress.\n\n"
        f"Links per message: <b>{values['max_batch_urls']}</b>\n\n"
        f"Downloads\nTarget: <b>{values['max_concurrent_downloads']}</b>\n"
        f"Allowed now: <b>{effective['download']}</b> · Running: {active['download']}\n\n"
        f"Extraction\nTarget: <b>{values['max_concurrent_extractions']}</b>\n"
        f"Allowed now: <b>{effective['extraction']}</b> · Running: {active['extraction']}\n\n"
        f"Merges\nTarget: <b>{values['max_concurrent_merges']}</b>\n"
        f"Allowed now: <b>{effective['merge']}</b> · Running: {active['merge']}\n\n"
        f"Uploads\nTarget: <b>{values['max_concurrent_uploads']}</b>\n"
        f"Allowed now: <b>{effective['upload']}</b> · Running: {active['upload']}\n\n"
        f"Reason: {escape(reason)}"
    )


def build_performance_keyboard(values: dict[str, object]) -> InlineKeyboardMarkup:
    aliases = {
        "max_batch_urls": "batch", "max_concurrent_downloads": "down",
        "max_concurrent_extractions": "extract", "max_concurrent_merges": "merge",
        "max_concurrent_uploads": "upload",
    }
    labels = {
        "max_batch_urls": "Links per message", "max_concurrent_downloads": "Downloads",
        "max_concurrent_extractions": "Analysis", "max_concurrent_merges": "Finalizing",
        "max_concurrent_uploads": "Telegram sends",
    }
    builder = InlineKeyboardBuilder()
    for key, alias in aliases.items():
        builder.row(
            InlineKeyboardButton(text=f"➖ {labels[key]}", callback_data=f"perf:{alias}:-1"),
            InlineKeyboardButton(text=f"Current: {values[key]}", callback_data="noop"),
            InlineKeyboardButton(text=f"➕ {labels[key]}", callback_data=f"perf:{alias}:1"),
        )
        builder.row(InlineKeyboardButton(
            text=f"↺ Reset {labels[key].lower()} to default", callback_data=f"perfreset:{alias}"
        ))
    builder.row(InlineKeyboardButton(text="⚙️ Advanced limits", callback_data="perfadvanced:show"))
    builder.row(InlineKeyboardButton(text="↩️ Back to admin", callback_data="admin:status"))
    return builder.as_markup()


def build_performance_advanced_text(values: dict[str, object]) -> str:
    bandwidth = int(values["bandwidth_ceiling"])
    bandwidth_text = "Unlimited" if bandwidth == 0 else f"{bandwidth / 1024**2:g} MiB/s per download"
    return (
        "⚙️ <b>Advanced limits</b>\n\n"
        f"Per-user active/queued limit: <b>{values['max_queued_jobs_per_user']}</b>\n"
        f"Global active/queued limit: <b>{values['max_total_queued_jobs']}</b>\n"
        f"Bandwidth ceiling: <b>{bandwidth_text}</b>\n\n"
        "Changes apply to new admission/processes. Healthy active work is not killed."
    )


def build_performance_advanced_keyboard(values: dict[str, object]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, alias, key in (
        ("User queue", "userq", "max_queued_jobs_per_user"),
        ("Global queue", "globalq", "max_total_queued_jobs"),
        ("Bandwidth", "bw", "bandwidth_ceiling"),
    ):
        shown = values[key]
        if key == "bandwidth_ceiling":
            shown = "∞" if int(shown) == 0 else f"{int(shown) // 1024**2}M"
        builder.row(
            InlineKeyboardButton(text=f"➖ {label}", callback_data=f"perf:{alias}:-1"),
            InlineKeyboardButton(text=f"Current: {shown}", callback_data="noop"),
            InlineKeyboardButton(text=f"➕ {label}", callback_data=f"perf:{alias}:1"),
        )
        builder.row(InlineKeyboardButton(
            text=f"↺ Reset {label.lower()} to default", callback_data=f"perfreset:{alias}"
        ))
    builder.row(InlineKeyboardButton(text="↩️ Back to concurrency & limits", callback_data="admin:performance"))
    return builder.as_markup()


def build_resource_text(current: str) -> str:
    label = {
        "auto-shared": "Automatic · shared server",
        "auto-dedicated": "Automatic · dedicated server",
        "manual": "Manual limits",
    }.get(current, current)
    return (
        "🛡 <b>Resource safety mode</b>\n\n"
        f"Current mode: <b>{escape(label)}</b>\n\n"
        "<b>Automatic · shared server</b>\nProtects capacity for other services.\n\n"
        "<b>Automatic · dedicated server</b>\nUses more of the machine while retaining safety limits.\n\n"
        "<b>Manual limits</b>\nUses configured targets while core disk and memory safety protections remain active.\n\n"
        "Changes affect new work and do not terminate healthy downloads already running."
    )


def build_resource_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in (
        ("auto-shared", "Automatic · shared server"),
        ("auto-dedicated", "Automatic · dedicated server"),
        ("manual", "Manual limits"),
    ):
        builder.row(InlineKeyboardButton(
            text=("● " if current == value else "○ ") + label,
            callback_data=f"resource:{value}",
        ))
    builder.row(InlineKeyboardButton(text="↩️ Back to admin", callback_data="admin:status"))
    return builder.as_markup()


def build_users_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users[:40]:
        state = "✅" if user["is_allowed"] else "🛑"
        role = "👑" if user["is_admin"] else "👤"
        builder.row(InlineKeyboardButton(
            text=f"{state} {role} {user['user_id']}", callback_data=f"user:{user['user_id']}:view"
        ))
    builder.row(InlineKeyboardButton(text="↩️ Back to admin", callback_data="admin:status"))
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
    builder.row(InlineKeyboardButton(text="↩️ Back to users", callback_data="admin:users"))
    return text, builder.as_markup()
