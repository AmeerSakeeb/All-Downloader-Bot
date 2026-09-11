"""Exact output identity for cache reuse and active-job coalescing."""

import hashlib
import json
from typing import Optional

from core.models import MediaFormat, MediaSession
from services.canonicalization import canonicalize_media_url


def build_output_identity(
    session: MediaSession,
    primary: MediaFormat,
    audio: Optional[MediaFormat],
    send_mode: str,
    media_kind: str = "video",
) -> str:
    expected_container = primary.ext.lower()
    identity = {
        "canonical_source": canonicalize_media_url(session.canonical_url or session.url),
        "extractor": session.extractor,
        "media_id": session.media_id,
        "primary_fingerprint": primary.execution_snapshot()["fingerprint"],
        "audio_fingerprint": audio.execution_snapshot()["fingerprint"] if audio else None,
        "expected_container": expected_container,
        "send_mode": send_mode,
        "media_kind": media_kind,
        "collection_entry_index": session.collection_entry_index,
        "collection_entry_id": session.collection_entry_id,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
