from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.ffmpeg_helpers import parse_ffmpeg_progress
from tg_comment_uploader.media_compress import (
    CompressionError,
    CompressionPlan,
    CompressionProgress,
    MediaInfo,
    adjusted_video_bitrate,
    build_ffmpeg_pass_command,
    build_ffprobe_command,
    compress_video,
    parse_probe_json,
    plan_compression,
    probe_media,
)


def media_info(*, audio: bool = True, duration_us: int = 10_000_000) -> MediaInfo:
    return MediaInfo(
        duration_us=duration_us,
        video_stream_index=2,
        audio_stream_index=3 if audio else None,
        audio_channels=2 if audio else None,
    )


def compression_plan(*, audio: bool = True) -> CompressionPlan:
    return CompressionPlan(
        duration_us=10_000_000,
        target_bytes=900_000,
        video_stream_index=2,
        audio_stream_index=3 if audio else None,
        audio_channels=2 if audio else None,
        video_bitrate_bps=592_000 if audio else 720_000,
        audio_bitrate_bps=128_000 if audio else None,
    )


def truncate_file(path: Path, size: int) -> None:
    with path.open("wb") as file:
        file.truncate(size)


def test_parse_probe_json_skips_attached_picture_and_uses_longest_duration() -> None:
    raw = json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "duration": "100.0",
                    "disposition": {"attached_pic": 1},
                },
                {
                    "index": 2,
                    "codec_type": "video",
                    "duration": "12.500001",
                    "disposition": {"attached_pic": 0},
                },
                {
                    "index": 3,
                    "codec_type": "audio",
                    "duration": "13.0",
                    "channels": 6,
                },
            ],
            "format": {"duration": "12.8"},
        }
    )

    result = parse_probe_json(raw)

    assert result == MediaInfo(
        duration_us=13_000_000,
        video_stream_index=2,
        audio_stream_index=3,
        audio_channels=6,
    )


def test_parse_probe_json_falls_back_to_duration_ts_and_time_base() -> None:
    result = parse_probe_json(
        json.dumps(
            {
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "duration_ts": 301,
                        "time_base": "1/30",
                    }
                ],
                "format": {},
            }
        )
    )

    assert result.duration_us == 10_033_334


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"streams": [], "format": {"duration": "1"}}, "no usable video"),
        (
            {"streams": [{"index": 0, "codec_type": "video"}], "format": {}},
            "no finite positive media duration",
        ),
    ],
)
def test_parse_probe_json_rejects_unusable_media(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(CompressionError, match=message):
        parse_probe_json(json.dumps(payload))


def test_build_ffprobe_command_keeps_special_path_in_one_argument() -> None:
    source = Path("/videos/a file 'with quotes' & signs.mp4")

    command = build_ffprobe_command(source, ffprobe_binary="custom-ffprobe")

    assert command[0] == "custom-ffprobe"
    assert command[-1] == str(source)
    assert command.count(str(source)) == 1
    assert "-show_entries" in command


def test_probe_media_missing_ffprobe_suggests_dev_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = "missing-ffprobe"

    def missing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise FileNotFoundError(2, "No such file or directory", binary)

    monkeypatch.setattr("tg_comment_uploader.media_compress.subprocess.run", missing_run)

    with pytest.raises(CompressionError) as exc_info:
        probe_media(Path("/videos/source.mp4"), ffprobe_binary=binary)

    message = str(exc_info.value)
    assert repr(binary) in message
    assert "`just dev-shell`" in message
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_probe_media_other_os_error_is_not_reported_as_missing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = "unexecutable-ffprobe"

    def denied_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise PermissionError(13, "Permission denied", binary)

    monkeypatch.setattr("tg_comment_uploader.media_compress.subprocess.run", denied_run)

    with pytest.raises(CompressionError) as exc_info:
        probe_media(Path("/videos/source.mp4"), ffprobe_binary=binary)

    message = str(exc_info.value)
    assert f"failed to start ffprobe ({binary})" in message
    assert "Permission denied" in message
    assert "just dev-shell" not in message


def test_plan_compression_allocates_audio_and_video_from_target_size() -> None:
    plan = plan_compression(media_info(), target_bytes=900_000)

    assert plan.audio_bitrate_bps == 128_000
    assert plan.video_bitrate_bps == 592_000
    assert plan.audio_stream_index == 3


def test_plan_compression_uses_all_stream_budget_for_silent_video() -> None:
    plan = plan_compression(media_info(audio=False), target_bytes=900_000)

    assert plan.audio_bitrate_bps is None
    assert plan.video_bitrate_bps == 720_000


def test_plan_compression_reduces_audio_budget_when_total_bitrate_is_low() -> None:
    plan = plan_compression(
        media_info(duration_us=10_000_000),
        target_bytes=275_000,
    )

    assert plan.audio_bitrate_bps == 96_000
    assert plan.video_bitrate_bps == 124_000


def test_plan_compression_rejects_target_below_minimum_bitrates() -> None:
    with pytest.raises(CompressionError, match="too small to retain audio"):
        plan_compression(media_info(), target_bytes=180_000)


def test_adjusted_video_bitrate_uses_actual_to_target_ratio_and_headroom() -> None:
    result = adjusted_video_bitrate(
        1_000_000,
        actual_bytes=1_100_000,
        target_bytes=900_000,
    )

    assert result == 810_000
    assert result < 1_000_000


def test_build_first_pass_is_null_video_only_and_never_uses_fs(tmp_path: Path) -> None:
    source = tmp_path / "source file.mp4"
    output = tmp_path / "compressed.mp4"
    prefix = tmp_path / "pass log"

    command = build_ffmpeg_pass_command(
        source,
        output,
        prefix,
        compression_plan(),
        1,
    )

    assert command[command.index("-i") + 1] == str(source)
    assert "-nostdin" in command
    assert command[command.index("-map") + 1] == "0:2"
    assert command[command.index("-b:v") + 1] == "592000"
    assert command[command.index("-pass") + 1] == "1"
    assert command[-3:] == ["-f", "null", "/dev/null"]
    assert "-an" in command
    assert "-fs" not in command
    assert str(output) not in command


def test_build_second_pass_uses_h264_aac_yuv420p_and_faststart(tmp_path: Path) -> None:
    source = tmp_path / "source [special].mp4"
    output = tmp_path / "compressed.mp4"
    prefix = tmp_path / "pass"

    command = build_ffmpeg_pass_command(
        source,
        output,
        prefix,
        compression_plan(),
        2,
    )

    maps = [command[index + 1] for index, value in enumerate(command) if value == "-map"]
    assert maps == ["0:2", "0:3"]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "veryfast"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "128000"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert command[-1] == str(output)
    assert "-fs" not in command


def test_build_second_pass_disables_audio_for_silent_input(tmp_path: Path) -> None:
    command = build_ffmpeg_pass_command(
        tmp_path / "source.mp4",
        tmp_path / "compressed.mp4",
        tmp_path / "pass",
        compression_plan(audio=False),
        2,
    )

    assert "-an" in command
    assert "-c:a" not in command


def test_parse_ffmpeg_progress_clamps_fraction_and_marks_end_complete() -> None:
    assert parse_ffmpeg_progress(
        {"out_time_us": "2500000", "progress": "continue"}, 10_000_000
    ) == (2.5, 0.25)
    assert parse_ffmpeg_progress({"out_time_us": "12000000", "progress": "end"}, 10_000_000) == (
        12.0,
        1.0,
    )


def test_compress_video_missing_ffmpeg_suggests_dev_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    binary = "missing-ffmpeg"

    monkeypatch.setattr(
        "tg_comment_uploader.media_compress.probe_media",
        lambda path, **kwargs: media_info(audio=False),
    )

    def missing_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        del args, kwargs
        raise FileNotFoundError(2, "No such file or directory", binary)

    monkeypatch.setattr("tg_comment_uploader.media_compress.subprocess.Popen", missing_popen)

    with pytest.raises(CompressionError) as exc_info:
        compress_video(
            source,
            work_dir,
            hard_limit_bytes=1_000_000,
            target_bytes=900_000,
            ffmpeg_binary=binary,
        )

    message = str(exc_info.value)
    assert repr(binary) in message
    assert "`just dev-shell`" in message
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)
    assert not (work_dir / "compressed.mp4").exists()


def test_compress_video_retries_oversized_output_from_original_at_lower_bitrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source with spaces.mp4"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    commands: list[list[str]] = []
    pass_two_sizes = iter([1_000_001, 900_000])
    events: list[CompressionProgress] = []

    def fake_probe(path: Path, *, ffprobe_binary: str = "ffprobe") -> MediaInfo:
        del path, ffprobe_binary
        return media_info()

    def fake_run(command: list[str], **kwargs: Any) -> None:
        del kwargs
        commands.append(command)
        if command[command.index("-pass") + 1] == "2":
            truncate_file(Path(command[-1]), next(pass_two_sizes))

    monkeypatch.setattr("tg_comment_uploader.media_compress.probe_media", fake_probe)
    monkeypatch.setattr("tg_comment_uploader.media_compress._run_ffmpeg", fake_run)

    result = compress_video(
        source,
        work_dir,
        hard_limit_bytes=1_000_000,
        target_bytes=900_000,
        progress=events.append,
    )

    pass_two_commands = [
        command for command in commands if command[command.index("-pass") + 1] == "2"
    ]
    assert result == work_dir / "compressed.mp4"
    assert result.stat().st_size == 900_000
    assert len(commands) == 4
    assert all(command[command.index("-i") + 1] == str(source.resolve()) for command in commands)
    assert int(pass_two_commands[1][pass_two_commands[1].index("-b:v") + 1]) < int(
        pass_two_commands[0][pass_two_commands[0].index("-b:v") + 1]
    )
    assert {event.stage for event in events} == {
        "probing",
        "planning",
        "compressing",
        "validating",
    }


def test_compress_video_bounds_oversize_reencoding_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "tg_comment_uploader.media_compress.probe_media",
        lambda path, **kwargs: media_info(),
    )

    def fake_run(command: list[str], **kwargs: Any) -> None:
        del kwargs
        commands.append(command)
        if command[command.index("-pass") + 1] == "2":
            truncate_file(Path(command[-1]), 1_000_001)

    monkeypatch.setattr("tg_comment_uploader.media_compress._run_ffmpeg", fake_run)

    with pytest.raises(CompressionError, match="after 2 attempts"):
        compress_video(
            source,
            work_dir,
            hard_limit_bytes=1_000_000,
            target_bytes=900_000,
            max_attempts=2,
        )

    assert len(commands) == 4
    assert not (work_dir / "compressed.mp4").exists()


def test_compress_video_rejects_empty_output_without_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "tg_comment_uploader.media_compress.probe_media",
        lambda path, **kwargs: media_info(),
    )

    def fake_run(command: list[str], **kwargs: Any) -> None:
        del kwargs
        commands.append(command)
        if command[command.index("-pass") + 1] == "2":
            Path(command[-1]).touch()

    monkeypatch.setattr("tg_comment_uploader.media_compress._run_ffmpeg", fake_run)

    with pytest.raises(CompressionError, match="missing, not regular, or empty"):
        compress_video(
            source,
            work_dir,
            hard_limit_bytes=1_000_000,
            target_bytes=900_000,
        )

    assert len(commands) == 2
    assert not (work_dir / "compressed.mp4").exists()


def test_compress_video_validates_target_and_work_directory(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(ValueError, match="below hard_limit_bytes"):
        compress_video(source, work_dir, hard_limit_bytes=1_000, target_bytes=1_000)

    with pytest.raises(CompressionError, match="must not be inside"):
        nested_source = work_dir / "nested.mp4"
        nested_source.write_bytes(b"source")
        compress_video(
            nested_source,
            work_dir,
            hard_limit_bytes=1_000,
            target_bytes=900,
        )


def _ffmpeg_with_libx264_available() -> bool:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "libx264" in result.stdout


@pytest.mark.skipif(
    not _ffmpeg_with_libx264_available(),
    reason="ffmpeg, ffprobe, and libx264 are required",
)
def test_compress_video_with_real_ffmpeg(tmp_path: Path) -> None:
    source = tmp_path / "real source.mp4"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    generated = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=128x72:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr

    events: list[CompressionProgress] = []
    output = compress_video(
        source,
        work_dir,
        hard_limit_bytes=300_000,
        target_bytes=250_000,
        progress=events.append,
    )

    assert 0 < output.stat().st_size <= 300_000
    assert probe_media(output).video_stream_index == 0
    assert any(event.stage == "compressing" and event.fraction == 1.0 for event in events)
    assert not list(work_dir.glob("ffmpeg-pass-*"))
