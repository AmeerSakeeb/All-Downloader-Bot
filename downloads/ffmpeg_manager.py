"""Strict local-only FFprobe and lossless FFmpeg stream-copy operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from core.exceptions import DownloadError, StreamIncompatibleError
from downloads.process_supervisor import ProcessSupervisor


class FFmpegManager:
    def __init__(
        self,
        supervisor: Optional[ProcessSupervisor] = None,
        *,
        probe_timeout: float = 15.0,
        merge_timeout: float = 300.0,
        max_diagnostic_bytes: int = 1024 * 1024,
    ):
        self.supervisor = supervisor or ProcessSupervisor()
        self.probe_timeout = probe_timeout
        self.merge_timeout = merge_timeout
        self.max_diagnostic_bytes = max_diagnostic_bytes

    @staticmethod
    def _local_file(path: Path, *, must_exist: bool = True) -> Path:
        if not isinstance(path, Path):
            raise TypeError("Media paths must be pathlib.Path instances")
        raw = str(path)
        if "://" in raw or raw.startswith("\\\\"):
            raise DownloadError("Only local filesystem paths are accepted")
        resolved = path.resolve(strict=must_exist)
        if must_exist and (not resolved.exists() or not resolved.is_file()):
            raise FileNotFoundError(f"Local media file does not exist: {path.name}")
        return resolved

    async def probe_file(
        self, file_path: Path, *, job_id: str = "probe", stage: str | None = None
    ) -> dict[str, Any]:
        local_path = self._local_file(file_path)
        try:
            result = await self.supervisor.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-show_format",
                    str(local_path),
                ],
                job_id=job_id,
                stage=stage or f"ffprobe-{id(asyncio.current_task())}",
                timeout=self.probe_timeout,
                max_output_bytes=self.max_diagnostic_bytes,
            )
        except FileNotFoundError as error:
            raise DownloadError("ffprobe executable is unavailable") from error
        except asyncio.TimeoutError as error:
            raise DownloadError(f"ffprobe timed out for {local_path.name}") from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise DownloadError(
                f"ffprobe failed for {local_path.name} (exit {result.returncode}): {detail}"
            )
        try:
            data = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DownloadError("ffprobe returned malformed JSON") from error
        if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
            raise DownloadError("ffprobe JSON did not contain a streams array")
        if "format" in data and not isinstance(data["format"], dict):
            raise DownloadError("ffprobe JSON contained an invalid format object")
        for stream in data["streams"]:
            if not isinstance(stream, dict) or not isinstance(stream.get("codec_type"), str):
                raise DownloadError("ffprobe returned an invalid stream entry")
        return data

    async def merge_streams(
        self, video_path: Path, audio_path: Path, output_path: Path, *, job_id: str | None = None
    ) -> Path:
        video = self._local_file(video_path)
        audio = self._local_file(audio_path)
        if not isinstance(output_path, Path) or "://" in str(output_path):
            raise DownloadError("Output must be a local pathlib.Path")
        output_parent = output_path.parent.resolve(strict=True)
        output_base = output_parent / output_path.stem

        video_probe, audio_probe = await asyncio.gather(
            self.probe_file(video, job_id=job_id or output_parent.name, stage="ffprobe-video"),
            self.probe_file(audio, job_id=job_id or output_parent.name, stage="ffprobe-audio"),
        )
        video_codec = self._first_codec(video_probe, "video")
        audio_codec = self._first_codec(audio_probe, "audio")
        if not video_codec or not audio_codec:
            raise DownloadError("The selected inputs do not contain video and audio streams")

        requested = output_path.suffix.lower().lstrip(".")
        candidates = self.lossless_container_candidates(video_codec, audio_codec, requested)
        failures: list[str] = []
        for container in candidates:
            candidate = output_base.with_suffix(f".{container}")
            candidate.unlink(missing_ok=True)
            command = [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                str(candidate),
            ]
            try:
                result = await self.supervisor.run(
                    command,
                    job_id=job_id or output_parent.name,
                    stage="merge",
                    timeout=self.merge_timeout,
                    max_output_bytes=self.max_diagnostic_bytes,
                )
                if result.returncode != 0:
                    detail = result.stderr.decode("utf-8", errors="replace")[-1200:].strip()
                    failures.append(f"{container}: ffmpeg exit {result.returncode}: {detail}")
                    candidate.unlink(missing_ok=True)
                    continue
                merged = await self.probe_file(
                    candidate, job_id=job_id or output_parent.name, stage="ffprobe-output"
                )
                if (
                    self._first_codec(merged, "video") != video_codec
                    or self._first_codec(merged, "audio") != audio_codec
                ):
                    failures.append(f"{container}: post-merge codec identity changed")
                    candidate.unlink(missing_ok=True)
                    continue
                return candidate
            except (DownloadError, asyncio.TimeoutError) as error:
                candidate.unlink(missing_ok=True)
                failures.append(f"{container}: {error}")

        output_path.unlink(missing_ok=True)
        summary = "; ".join(failures)[-2000:]
        raise StreamIncompatibleError(video_codec, audio_codec, summary or "no container")

    @staticmethod
    def _first_codec(probe: dict[str, Any], stream_type: str) -> Optional[str]:
        for stream in probe["streams"]:
            if stream.get("codec_type") == stream_type:
                value = stream.get("codec_name")
                return str(value).lower() if value else None
        return None

    @staticmethod
    def lossless_container_candidates(
        video_codec: str, audio_codec: str, requested: str = ""
    ) -> list[str]:
        """Order likely muxers but leave final compatibility judgment to FFmpeg."""
        video_codec = video_codec.lower()
        audio_codec = audio_codec.lower()
        likely: list[str] = []
        if video_codec in {"h264", "hevc", "h265", "av1", "mpeg4"} and audio_codec in {
            "aac",
            "mp3",
            "ac3",
            "alac",
            "opus",
        }:
            likely.append("mp4")
        if video_codec in {"vp8", "vp9", "av1"} and audio_codec in {"opus", "vorbis"}:
            likely.append("webm")
        if requested in {"mp4", "webm", "mkv"}:
            likely.insert(0, requested)
        likely.append("mkv")
        return list(dict.fromkeys(likely))
