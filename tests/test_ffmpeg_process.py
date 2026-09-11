import asyncio
import json
from pathlib import Path

import pytest

from core.exceptions import DownloadError, StreamIncompatibleError
from downloads.ffmpeg_manager import FFmpegManager
from downloads.process_supervisor import ProcessResult, ProcessSupervisor


class FakeSupervisor:
    def __init__(self, fail_merge=False, mismatch=False):
        self.commands = []
        self.fail_merge = fail_merge
        self.mismatch = mismatch

    async def run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"merged")
            return ProcessResult(1 if self.fail_merge else 0, b"", b"mux failed")
        name = Path(command[-1]).name
        if "audio" in name:
            streams = [{"codec_type": "audio", "codec_name": "aac"}]
        elif "merged" in name:
            streams = [
                {"codec_type": "video", "codec_name": "vp9" if self.mismatch else "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        else:
            streams = [{"codec_type": "video", "codec_name": "h264"}]
        return ProcessResult(0, json.dumps({"streams": streams, "format": {}}).encode(), b"")


@pytest.mark.asyncio
async def test_ffprobe_parsing(tmp_path):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    manager = FFmpegManager(FakeSupervisor())
    assert (await manager.probe_file(source))["streams"][0]["codec_name"] == "h264"


@pytest.mark.asyncio
async def test_ffprobe_rejects_invalid_structure(tmp_path):
    class Bad:
        async def run(self, *args, **kwargs):
            return ProcessResult(0, b'{"format": {}}', b"")

    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")
    with pytest.raises(DownloadError):
        await FFmpegManager(Bad()).probe_file(source)


@pytest.mark.asyncio
async def test_ffmpeg_stream_copy_and_explicit_maps(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    supervisor = FakeSupervisor()
    result = await FFmpegManager(supervisor).merge_streams(video, audio, tmp_path / "merged.mp4")
    command = next(cmd for cmd in supervisor.commands if cmd[0] == "ffmpeg")
    assert result.exists()
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command.count("-map") == 2
    assert not any(arg.startswith("lib") for arg in command)


@pytest.mark.asyncio
async def test_post_merge_codec_verification(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    with pytest.raises(StreamIncompatibleError):
        await FFmpegManager(FakeSupervisor(mismatch=True)).merge_streams(
            video, audio, tmp_path / "merged.mp4"
        )


@pytest.mark.asyncio
async def test_incompatible_lossless_mux_removes_partial(tmp_path):
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    output = tmp_path / "merged.mp4"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    with pytest.raises(StreamIncompatibleError):
        await FFmpegManager(FakeSupervisor(fail_merge=True)).merge_streams(video, audio, output)
    assert not output.exists()


@pytest.mark.asyncio
async def test_process_term_wait_kill_and_reap(monkeypatch):
    events = []

    class Process:
        pid = 42
        returncode = None

        def __init__(self):
            self.calls = 0

        async def wait(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            self.returncode = -9
            return -9

    supervisor = ProcessSupervisor(terminate_grace_seconds=0.01)
    monkeypatch.setattr(supervisor, "_send_term", lambda pid: events.append("term"))

    async def kill(pid):
        events.append("kill")

    monkeypatch.setattr(supervisor, "kill_process_tree", kill)
    process = Process()
    assert await supervisor._terminate_handle(process) == -9
    assert events == ["term", "kill"]
