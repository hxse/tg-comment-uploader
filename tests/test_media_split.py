import io
import json
import shutil
import subprocess
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.media_split import (
    MediaProbe,
    MediaSplitError,
    SplitPlan,
    SplitPoint,
    SplitProgress,
    build_segment_command,
    parse_ffmpeg_progress,
    parse_probe_payload,
    plan_split,
    probe_media,
    split_video,
)


class FakePopen:
    def __init__(
        self,
        *,
        stdout: str,
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


def probe_with_positions(*positions: int) -> MediaProbe:
    points = tuple(SplitPoint(Decimal(index), position) for index, position in enumerate(positions))
    return MediaProbe(
        points=points,
        packet_bytes=positions[-1],
        duration=Decimal(len(positions) - 1),
        frame_rate=Fraction(25, 1),
    )


def probe_payload() -> dict[str, Any]:
    return {
        "format": {"start_time": "0.000000", "duration": "4.000000"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "avg_frame_rate": "25/1",
                "r_frame_rate": "25/1",
            },
            {"index": 1, "codec_type": "audio", "avg_frame_rate": "0/0"},
        ],
        "packets": [
            {
                "stream_index": 0,
                "pts_time": "0.000000",
                "dts_time": "0.000000",
                "size": "30",
                "flags": "K__",
            },
            {
                "stream_index": 1,
                "pts_time": "0.000000",
                "dts_time": "0.000000",
                "size": "10",
                "flags": "K__",
            },
            {
                "stream_index": 0,
                "pts_time": "1.000000",
                "dts_time": "1.000000",
                "size": "20",
                "flags": "___",
            },
            {
                "stream_index": 1,
                "pts_time": "1.000000",
                "dts_time": "1.000000",
                "size": "10",
                "flags": "K__",
            },
            {
                "stream_index": 0,
                "pts_time": "2.000000",
                "dts_time": "2.000000",
                "size": "30",
                "flags": "K__",
            },
            {
                "stream_index": 1,
                "pts_time": "2.000000",
                "dts_time": "2.000000",
                "size": "10",
                "flags": "K__",
            },
            {
                "stream_index": 0,
                "pts_time": "3.000000",
                "dts_time": "3.000000",
                "size": "20",
                "flags": "___",
            },
            {
                "stream_index": 1,
                "pts_time": "3.000000",
                "dts_time": "3.000000",
                "size": "10",
                "flags": "K__",
            },
        ],
    }


def test_parse_probe_payload_counts_all_streams_at_video_keyframes() -> None:
    probe = parse_probe_payload(probe_payload())

    assert probe.packet_bytes == 140
    assert probe.duration == Decimal("4.000000")
    assert probe.frame_rate == Fraction(25, 1)
    assert probe.points == (
        SplitPoint(Decimal(0), 0),
        SplitPoint(Decimal("2.000000"), 70),
        SplitPoint(Decimal("4.000000"), 140),
    )


def test_parse_probe_payload_rejects_missing_internal_video_keyframes() -> None:
    payload = probe_payload()
    for packet in payload["packets"]:
        if packet["stream_index"] == 0 and packet["pts_time"] != "0.000000":
            packet["flags"] = "___"

    with pytest.raises(MediaSplitError, match="no internal video keyframes"):
        parse_probe_payload(payload)


def test_probe_media_reports_missing_ffprobe_with_dev_shell_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_tool(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("No such file or directory")

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", missing_tool)

    with pytest.raises(MediaSplitError) as exc_info:
        probe_media(Path("/videos/source.mp4"), ffprobe_binary="missing-ffprobe")

    message = str(exc_info.value)
    assert "missing-ffprobe" in message
    assert "just dev-shell" in message


def test_parse_ffmpeg_progress_uses_duration_and_clamps_fraction() -> None:
    assert parse_ffmpeg_progress(
        {"out_time_us": "1000000", "progress": "continue"},
        Decimal("4"),
    ) == (1.0, 0.25)
    assert parse_ffmpeg_progress(
        {"out_time": "00:00:05.000000", "progress": "end"},
        Decimal("4"),
    ) == (5.0, 1.0)
    assert parse_ffmpeg_progress({"progress": "end"}, Decimal("4")) == (None, 1.0)


def test_plan_split_uses_fewest_parts_before_balancing() -> None:
    plan = plan_split(
        probe_with_positions(0, 20, 45, 60, 100),
        source_size_bytes=100,
        target_bytes=60,
    )

    assert plan.part_count == 2
    assert plan.split_times == (Decimal(2),)
    assert plan.estimated_part_bytes == (45, 55)
    assert plan.minimized_max_part_bytes == 55


def test_plan_split_adds_part_when_no_two_part_keyframe_partition_is_safe() -> None:
    plan = plan_split(
        probe_with_positions(0, 40, 80, 120),
        source_size_bytes=120,
        target_bytes=70,
    )

    assert plan.part_count == 3
    assert plan.split_times == (Decimal(1), Decimal(2))
    assert plan.estimated_part_bytes == (40, 40, 40)


def test_plan_split_uses_source_size_for_initial_part_lower_bound() -> None:
    plan = plan_split(
        probe_with_positions(0, 25, 50, 75, 100),
        source_size_bytes=181,
        target_bytes=90,
    )

    assert plan.part_count == 3
    assert max(plan.estimated_part_bytes) <= 50


def test_plan_split_reports_keyframe_interval_larger_than_target() -> None:
    with pytest.raises(MediaSplitError, match="keyframe interval"):
        plan_split(
            probe_with_positions(0, 70, 100),
            source_size_bytes=100,
            target_bytes=60,
        )


def test_build_segment_command_is_argument_array_and_disables_stdin() -> None:
    source = Path("/videos/a file [x].mp4")
    output = Path("/videos/.tg-comment-uploader-work/a file [x] part-%04d.mp4")
    plan = SplitPlan(
        split_times=(Decimal("1.25"), Decimal("2.5")),
        estimated_part_bytes=(10, 11, 12),
        target_bytes=20,
        minimized_max_part_bytes=12,
    )

    command = build_segment_command(
        source,
        output,
        plan,
        ffmpeg_binary="/nix/store/ffmpeg/bin/ffmpeg",
        frame_rate=Fraction(25, 1),
    )

    assert isinstance(command, list)
    assert command[0] == "/nix/store/ffmpeg/bin/ffmpeg"
    assert "-nostdin" in command
    assert command[command.index("-i") + 1] == str(source)
    assert command[command.index("-segment_times") + 1] == "1.25,2.5"
    assert command[command.index("-segment_time_delta") + 1] == "0.02"
    assert command[command.index("-reference_stream") + 1] == "v:0"
    assert command[-1] == str(output)


def test_split_video_streams_structured_progress_from_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 101)
    work_dir = tmp_path / ".tg-comment-uploader-work"
    events: list[SplitProgress] = []
    process = FakePopen(
        stdout=(
            "out_time_us=1000000\nprogress=continue\nout_time=00:00:02.000000\nprogress=continue\n"
        ),
        stderr="non-fatal diagnostic\n",
    )

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "-show_packets" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(probe_payload()), "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"streams": [{"index": 0, "codec_type": "video"}]}),
            "",
        )

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        pattern = command[-1]
        Path(pattern % 1).write_bytes(b"a" * 75)
        Path(pattern % 2).write_bytes(b"b" * 75)
        return process

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.Popen", fake_popen)

    result = split_video(
        source,
        work_dir,
        hard_limit_bytes=100,
        target_bytes=90,
        progress=events.append,
    )

    splitting = [event for event in events if event.stage == "splitting"]
    assert result.plan.part_count == 2
    assert {event.stage for event in events} == {
        "probing",
        "planning",
        "splitting",
        "validating",
    }
    assert [event.processed_seconds for event in splitting] == [0.0, 1.0, 2.0, 4.0]
    assert [event.fraction for event in splitting] == [0.0, 0.25, 0.5, 1.0]
    assert all(event.attempt == 1 for event in splitting)
    assert all(event.max_attempts == 3 for event in splitting)
    assert all(event.part_count == 2 for event in splitting)
    assert all(event.duration_seconds == 4.0 for event in splitting)
    assert process.wait_calls == 1
    assert process.terminate_calls == 0


def test_split_video_interrupt_terminates_and_reaps_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 101)
    work_dir = tmp_path / ".tg-comment-uploader-work"
    process = FakePopen(stdout="out_time_us=1000000\nprogress=continue\n")

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, json.dumps(probe_payload()), "")

    def fake_popen(command: list[str], **kwargs: Any) -> Any:
        del kwargs
        pattern = command[-1]
        Path(pattern % 1).write_bytes(b"partial")
        return process

    def interrupt(event: SplitProgress) -> None:
        if event.stage == "splitting":
            raise KeyboardInterrupt

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.Popen", fake_popen)

    with pytest.raises(KeyboardInterrupt):
        split_video(
            source,
            work_dir,
            hard_limit_bytes=100,
            target_bytes=90,
            progress=interrupt,
        )

    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
    assert list(work_dir.glob("source part-*.mp4")) == []


def test_split_video_preserves_ffmpeg_stderr_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 101)
    work_dir = tmp_path / ".tg-comment-uploader-work"
    process = FakePopen(
        stdout="progress=end\n",
        stderr="specific stream-copy failure\nsecond diagnostic line\n",
        returncode=1,
    )

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, json.dumps(probe_payload()), "")

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr(
        "tg_comment_uploader.media_split.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(MediaSplitError) as exc_info:
        split_video(
            source,
            work_dir,
            hard_limit_bytes=100,
            target_bytes=90,
        )

    message = str(exc_info.value)
    assert "specific stream-copy failure" in message
    assert "second diagnostic line" in message
    assert process.wait_calls == 1
    assert process.terminate_calls == 0


def test_split_video_runs_stream_copy_and_validates_every_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.final 100% [x].mp4"
    source.write_bytes(b"x" * 101)
    work_dir = tmp_path / ".tg-comment-uploader-work"
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["stdin"] is subprocess.DEVNULL
        if command[0] == "ffprobe" and "-show_packets" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(probe_payload()), "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"streams": [{"index": 0, "codec_type": "video"}]}),
            "",
        )

    def fake_ffmpeg(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        pattern = command[-1]
        Path(pattern % 1).write_bytes(b"a" * 75)
        Path(pattern % 2).write_bytes(b"b" * 75)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr("tg_comment_uploader.media_split._run_ffmpeg", fake_ffmpeg)

    result = split_video(
        source,
        work_dir,
        hard_limit_bytes=100,
        target_bytes=90,
    )

    assert result.attempts == 1
    assert result.parts == (
        work_dir / "source.final 100% [x] part-0001.mp4",
        work_dir / "source.final 100% [x] part-0002.mp4",
    )
    assert sum(command[0] == "ffprobe" for command in commands) == 3
    ffmpeg_command = next(command for command in commands if command[0] == "ffmpeg")
    assert "-nostdin" in ffmpeg_command
    assert ffmpeg_command[ffmpeg_command.index("-i") + 1] == str(source)
    assert ffmpeg_command[ffmpeg_command.index("-c") + 1] == "copy"
    assert ffmpeg_command[-1] == str(work_dir / "source.final 100%% [x] part-%04d.mp4")


def test_split_video_replans_all_parts_after_actual_size_exceeds_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 180)
    work_dir = tmp_path / ".tg-comment-uploader-work"
    ffmpeg_attempts = 0

    payload = {
        "format": {"start_time": "0", "duration": "6"},
        "streams": [{"index": 0, "codec_type": "video", "avg_frame_rate": "25/1"}],
        "packets": [
            {
                "stream_index": 0,
                "pts_time": str(timestamp),
                "dts_time": str(timestamp),
                "size": "30",
                "flags": "K__",
            }
            for timestamp in range(6)
        ],
    }

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal ffmpeg_attempts
        if command[0] == "ffprobe" and "-show_packets" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"streams": [{"index": 0, "codec_type": "video"}]}),
            "",
        )

    def fake_ffmpeg(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal ffmpeg_attempts
        del kwargs
        ffmpeg_attempts += 1
        pattern = command[-1]
        split_count = command[command.index("-segment_times") + 1].count(",") + 2
        for index in range(1, split_count + 1):
            size = 101 if ffmpeg_attempts == 1 and index == 1 else 70
            Path(pattern % index).write_bytes(b"x" * size)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr("tg_comment_uploader.media_split._run_ffmpeg", fake_ffmpeg)

    result = split_video(
        source,
        work_dir,
        hard_limit_bytes=100,
        target_bytes=90,
    )

    assert result.attempts == 2
    assert ffmpeg_attempts == 2
    assert all(part.stat().st_size <= 100 for part in result.parts)
    assert not (work_dir / f"source part-{len(result.parts) + 1:04d}.mp4").exists()


def test_split_video_removes_outputs_when_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 101)
    work_dir = tmp_path / ".tg-comment-uploader-work"

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffprobe" and "-show_packets" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(probe_payload()), "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"streams": []}), "")

    def fake_ffmpeg(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        pattern = command[-1]
        Path(pattern % 1).write_bytes(b"a" * 75)
        Path(pattern % 2).write_bytes(b"b" * 75)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("tg_comment_uploader.media_split.subprocess.run", fake_run)
    monkeypatch.setattr("tg_comment_uploader.media_split._run_ffmpeg", fake_ffmpeg)

    with pytest.raises(MediaSplitError, match="usable video stream"):
        split_video(
            source,
            work_dir,
            hard_limit_bytes=100,
            target_bytes=90,
        )

    assert list(work_dir.glob("source part-*.mp4")) == []


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are not available",
)
def test_split_video_with_real_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None
    assert ffprobe is not None
    source = tmp_path / "real.final 100% source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=10:duration=4",
            "-c:v",
            "mpeg4",
            "-g",
            "5",
            "-q:v",
            "5",
            "-an",
            str(source),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    source_size = source.stat().st_size
    events: list[SplitProgress] = []

    result = split_video(
        source,
        tmp_path / ".tg-comment-uploader-work",
        hard_limit_bytes=source_size,
        target_bytes=source_size // 2,
        progress=events.append,
        ffmpeg_binary=ffmpeg,
        ffprobe_binary=ffprobe,
    )

    assert result.plan.part_count >= 2
    assert result.parts[0].name == "real.final 100% source part-0001.mp4"
    assert all(0 < part.stat().st_size <= source_size for part in result.parts)
    assert any(event.stage == "splitting" and event.fraction == 1.0 for event in events)
