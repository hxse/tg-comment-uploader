from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Callable, Literal

from .ffmpeg_helpers import (
    emit_progress,
    is_attached_picture,
    parse_ffmpeg_progress,
    terminate_process,
)

DEFAULT_AUDIO_BITRATE_BPS = 128_000
MONO_AUDIO_BITRATE_BPS = 64_000
MIN_AUDIO_BITRATE_BPS = 48_000
MIN_VIDEO_BITRATE_BPS = 100_000
BITRATE_CORRECTION_PERCENT = 99
DEFAULT_MAX_ATTEMPTS = 3
X264_PRESET = "veryfast"

CompressionStage = Literal["probing", "planning", "compressing", "validating"]


class CompressionError(Exception):
    """A deterministic media-compression failure with a user-facing message."""


@dataclass(frozen=True)
class MediaInfo:
    duration_us: int
    video_stream_index: int
    audio_stream_index: int | None
    audio_channels: int | None

    @property
    def duration_seconds(self) -> float:
        return self.duration_us / 1_000_000


@dataclass(frozen=True)
class CompressionPlan:
    duration_us: int
    target_bytes: int
    video_stream_index: int
    audio_stream_index: int | None
    audio_channels: int | None
    video_bitrate_bps: int
    audio_bitrate_bps: int | None


@dataclass(frozen=True)
class CompressionProgress:
    stage: CompressionStage
    attempt: int | None = None
    max_attempts: int | None = None
    pass_number: int | None = None
    encoded_seconds: float | None = None
    duration_seconds: float | None = None
    fraction: float | None = None


ProgressCallback = Callable[[CompressionProgress], None]


def build_ffprobe_command(source: Path, *, ffprobe_binary: str = "ffprobe") -> list[str]:
    """Build an argv-safe ffprobe command for the fields used by compression."""
    return [
        ffprobe_binary,
        "-v",
        "error",
        "-of",
        "json",
        "-show_entries",
        (
            "format=duration:"
            "stream=index,codec_type,duration,duration_ts,time_base,channels:"
            "stream_disposition=attached_pic"
        ),
        str(source),
    ]


def parse_probe_json(raw: str) -> MediaInfo:
    """Parse ffprobe JSON and conservatively choose duration and primary streams."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompressionError(f"ffprobe returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise CompressionError("ffprobe JSON root must be an object")

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise CompressionError("ffprobe did not return a streams array")

    video_stream: dict[str, object] | None = None
    audio_stream: dict[str, object] | None = None
    for item in streams:
        if not isinstance(item, dict):
            continue
        codec_type = item.get("codec_type")
        if codec_type == "video" and video_stream is None and not is_attached_picture(item):
            video_stream = item
        elif codec_type == "audio" and audio_stream is None:
            audio_stream = item

    if video_stream is None:
        raise CompressionError("ffprobe found no usable video stream")

    video_index = _stream_index(video_stream, "video")
    audio_index = _stream_index(audio_stream, "audio") if audio_stream is not None else None

    duration_candidates: list[int] = []
    format_section = payload.get("format")
    if isinstance(format_section, dict):
        duration = _duration_value_to_us(format_section.get("duration"))
        if duration is not None:
            duration_candidates.append(duration)

    for stream in (video_stream, audio_stream):
        if stream is None:
            continue
        duration = _stream_duration_us(stream)
        if duration is not None:
            duration_candidates.append(duration)

    if not duration_candidates:
        raise CompressionError("ffprobe found no finite positive media duration")

    audio_channels = None
    if audio_stream is not None:
        channels = audio_stream.get("channels")
        if isinstance(channels, int) and not isinstance(channels, bool) and channels > 0:
            audio_channels = channels

    return MediaInfo(
        duration_us=max(duration_candidates),
        video_stream_index=video_index,
        audio_stream_index=audio_index,
        audio_channels=audio_channels,
    )


def probe_media(source: Path, *, ffprobe_binary: str = "ffprobe") -> MediaInfo:
    command = build_ffprobe_command(source, ffprobe_binary=ffprobe_binary)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise CompressionError(
            f"required media tool {ffprobe_binary!r} was not found; run `just dev-shell` and retry"
        ) from exc
    except OSError as exc:
        raise CompressionError(f"failed to start ffprobe ({ffprobe_binary}): {exc}") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise CompressionError(
            f"ffprobe failed for {source} with exit code {completed.returncode}: {detail}"
        )
    return parse_probe_json(completed.stdout)


def plan_compression(media: MediaInfo, target_bytes: int) -> CompressionPlan:
    """Allocate a target byte budget between one H.264 and one optional AAC stream."""
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    if media.duration_us <= 0:
        raise ValueError("media duration must be positive")

    total_bitrate_bps = target_bytes * 8 * 1_000_000 // media.duration_us
    audio_bitrate_bps = _choose_audio_bitrate(media, total_bitrate_bps)
    video_bitrate_bps = total_bitrate_bps - (audio_bitrate_bps or 0)
    if video_bitrate_bps < MIN_VIDEO_BITRATE_BPS:
        raise CompressionError(
            "compression target is too small for the media duration: "
            f"duration={media.duration_seconds:.3f}s, target={target_bytes:,} bytes, "
            f"available video bitrate={video_bitrate_bps:,} bit/s, "
            f"minimum={MIN_VIDEO_BITRATE_BPS:,} bit/s"
        )

    return CompressionPlan(
        duration_us=media.duration_us,
        target_bytes=target_bytes,
        video_stream_index=media.video_stream_index,
        audio_stream_index=media.audio_stream_index,
        audio_channels=media.audio_channels,
        video_bitrate_bps=video_bitrate_bps,
        audio_bitrate_bps=audio_bitrate_bps,
    )


def adjusted_video_bitrate(
    current_video_bitrate_bps: int,
    *,
    actual_bytes: int,
    target_bytes: int,
) -> int:
    """Reduce video bitrate using observed output size, with one percent extra headroom."""
    if current_video_bitrate_bps <= 0:
        raise ValueError("current_video_bitrate_bps must be positive")
    if actual_bytes <= 0:
        raise ValueError("actual_bytes must be positive")
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")

    corrected = (
        current_video_bitrate_bps * target_bytes * BITRATE_CORRECTION_PERCENT // actual_bytes // 100
    )
    return min(corrected, current_video_bitrate_bps - 1)


def build_ffmpeg_pass_command(
    source: Path,
    output: Path,
    passlog_prefix: Path,
    plan: CompressionPlan,
    pass_number: Literal[1, 2],
    *,
    ffmpeg_binary: str = "ffmpeg",
) -> list[str]:
    """Build one x264 two-pass command as an argument array, never a shell string."""
    if pass_number not in (1, 2):
        raise ValueError("pass_number must be 1 or 2")

    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-stats_period",
        "1",
        "-i",
        str(source),
        "-map",
        f"0:{plan.video_stream_index}",
        "-c:v",
        "libx264",
        "-preset",
        X264_PRESET,
        "-b:v",
        str(plan.video_bitrate_bps),
        "-pix_fmt",
        "yuv420p",
        "-pass",
        str(pass_number),
        "-passlogfile",
        str(passlog_prefix),
    ]

    if pass_number == 1:
        return [*command, "-an", "-sn", "-dn", "-f", "null", os.devnull]

    if plan.audio_stream_index is None:
        command.append("-an")
    else:
        if plan.audio_bitrate_bps is None:
            raise ValueError("an audio stream requires an audio bitrate")
        output_channels = 1 if plan.audio_channels == 1 else 2
        command.extend(
            [
                "-map",
                f"0:{plan.audio_stream_index}",
                "-c:a",
                "aac",
                "-b:a",
                str(plan.audio_bitrate_bps),
                "-ac",
                str(output_channels),
            ]
        )

    command.extend(
        [
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def compress_video(
    source: Path,
    work_dir: Path,
    hard_limit_bytes: int,
    target_bytes: int,
    *,
    progress: ProgressCallback | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
) -> Path:
    """Compress source from scratch until compressed.mp4 passes the actual-size limit."""
    source, work_dir = _validate_compression_inputs(
        source,
        work_dir,
        hard_limit_bytes=hard_limit_bytes,
        target_bytes=target_bytes,
        max_attempts=max_attempts,
    )
    output = work_dir / "compressed.mp4"

    emit_progress(progress, CompressionProgress(stage="probing"))
    media = probe_media(source, ffprobe_binary=ffprobe_binary)
    emit_progress(
        progress,
        CompressionProgress(stage="planning", duration_seconds=media.duration_seconds),
    )
    plan = plan_compression(media, target_bytes)

    last_actual_size: int | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            _remove_output(output)
            passlog_prefix = work_dir / f"ffmpeg-pass-{attempt}"
            _remove_pass_logs(passlog_prefix)
            try:
                for pass_number in (1, 2):
                    event = CompressionProgress(
                        stage="compressing",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        pass_number=pass_number,
                        encoded_seconds=0.0,
                        duration_seconds=media.duration_seconds,
                        fraction=0.0,
                    )
                    emit_progress(progress, event)
                    command = build_ffmpeg_pass_command(
                        source,
                        output,
                        passlog_prefix,
                        plan,
                        pass_number,
                        ffmpeg_binary=ffmpeg_binary,
                    )
                    _run_ffmpeg(
                        command,
                        source=source,
                        pass_number=pass_number,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        duration_us=media.duration_us,
                        progress=progress,
                    )
            finally:
                _remove_pass_logs(passlog_prefix)

            emit_progress(
                progress,
                CompressionProgress(
                    stage="validating",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    duration_seconds=media.duration_seconds,
                ),
            )
            actual_size = _output_size(output)
            last_actual_size = actual_size
            if actual_size <= hard_limit_bytes:
                probe_media(output, ffprobe_binary=ffprobe_binary)
                return output

            if attempt == max_attempts:
                break
            next_bitrate = adjusted_video_bitrate(
                plan.video_bitrate_bps,
                actual_bytes=actual_size,
                target_bytes=target_bytes,
            )
            if next_bitrate < MIN_VIDEO_BITRATE_BPS:
                raise CompressionError(
                    "compressed output is still too large, but the corrected video bitrate "
                    f"would be {next_bitrate:,} bit/s, below the supported minimum of "
                    f"{MIN_VIDEO_BITRATE_BPS:,} bit/s"
                )
            plan = replace(plan, video_bitrate_bps=next_bitrate)

        raise CompressionError(
            "compressed output remains above the hard upload limit after "
            f"{max_attempts} attempts: source={source}, actual={last_actual_size:,} bytes, "
            f"limit={hard_limit_bytes:,} bytes, target={target_bytes:,} bytes"
        )
    except BaseException:
        _remove_output(output)
        raise


def _choose_audio_bitrate(media: MediaInfo, total_bitrate_bps: int) -> int | None:
    if media.audio_stream_index is None:
        return None

    preferred = MONO_AUDIO_BITRATE_BPS if media.audio_channels == 1 else DEFAULT_AUDIO_BITRATE_BPS
    candidates = [
        bitrate
        for bitrate in (
            preferred,
            96_000,
            MONO_AUDIO_BITRATE_BPS,
            MIN_AUDIO_BITRATE_BPS,
        )
        if bitrate <= preferred
    ]
    for bitrate in dict.fromkeys(candidates):
        if total_bitrate_bps - bitrate >= MIN_VIDEO_BITRATE_BPS:
            return bitrate

    raise CompressionError(
        "compression target is too small to retain audio while reserving the minimum "
        f"video bitrate of {MIN_VIDEO_BITRATE_BPS:,} bit/s"
    )


def _stream_index(stream: dict[str, object], label: str) -> int:
    index = stream.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise CompressionError(f"ffprobe returned an invalid {label} stream index")
    return index


def _duration_value_to_us(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not duration.is_finite() or duration <= 0:
        return None
    return int((duration * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _stream_duration_us(stream: dict[str, object]) -> int | None:
    direct_duration = _duration_value_to_us(stream.get("duration"))
    if direct_duration is not None:
        return direct_duration

    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")
    if (
        not isinstance(duration_ts, int)
        or isinstance(duration_ts, bool)
        or duration_ts <= 0
        or not isinstance(time_base, str)
    ):
        return None
    try:
        numerator_text, denominator_text = time_base.split("/", maxsplit=1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (ValueError, TypeError):
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    microsecond_numerator = duration_ts * numerator * 1_000_000
    return (microsecond_numerator + denominator - 1) // denominator


def _run_ffmpeg(
    command: list[str],
    *,
    source: Path,
    pass_number: int,
    attempt: int,
    max_attempts: int,
    duration_us: int,
    progress: ProgressCallback | None,
) -> None:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise CompressionError(
            f"required media tool {command[0]!r} was not found; run `just dev-shell` and retry"
        ) from exc
    except OSError as exc:
        raise CompressionError(f"failed to start ffmpeg ({command[0]}): {exc}") from exc

    output_tail: deque[str] = deque(maxlen=200)
    fields: dict[str, str] = {}
    try:
        if process.stdout is None:
            raise CompressionError("ffmpeg stdout pipe was not created")
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            output_tail.append(line)
            key, separator, value = line.partition("=")
            if separator and key.replace("_", "").isalnum():
                fields[key] = value
            if key == "progress" and separator:
                encoded_seconds, fraction = parse_ffmpeg_progress(fields, duration_us)
                emit_progress(
                    progress,
                    CompressionProgress(
                        stage="compressing",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        pass_number=pass_number,
                        encoded_seconds=encoded_seconds,
                        duration_seconds=duration_us / 1_000_000,
                        fraction=fraction,
                    ),
                )
                fields = {}
        return_code = process.wait()
    except BaseException:
        terminate_process(process)
        raise

    if return_code != 0:
        detail = "\n".join(output_tail).strip() or "no diagnostic output"
        raise CompressionError(
            f"ffmpeg pass {pass_number}/2 failed for {source} on attempt {attempt}/"
            f"{max_attempts} with exit code {return_code}: {detail}"
        )


def _validate_compression_inputs(
    source: Path,
    work_dir: Path,
    *,
    hard_limit_bytes: int,
    target_bytes: int,
    max_attempts: int,
) -> tuple[Path, Path]:
    if hard_limit_bytes <= 0:
        raise ValueError("hard_limit_bytes must be positive")
    if target_bytes <= 0 or target_bytes >= hard_limit_bytes:
        raise ValueError("target_bytes must be positive and below hard_limit_bytes")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if work_dir.is_symlink():
        raise CompressionError(f"compression work directory must not be a symlink: {work_dir}")
    try:
        source = source.resolve(strict=True)
        work_dir = work_dir.resolve(strict=True)
    except OSError as exc:
        raise CompressionError(f"failed to resolve compression paths: {exc}") from exc
    if not source.is_file():
        raise CompressionError(f"compression source is not a regular file: {source}")
    if source.stat().st_size <= 0:
        raise CompressionError(f"compression source is empty: {source}")
    if not work_dir.is_dir():
        raise CompressionError(f"compression work path is not a directory: {work_dir}")
    if source == work_dir or work_dir in source.parents:
        raise CompressionError("compression source must not be inside the work directory")
    return source, work_dir


def _output_size(output: Path) -> int:
    try:
        stat_result = output.stat()
    except OSError as exc:
        raise CompressionError(f"failed to inspect compressed output {output}: {exc}") from exc
    if not output.is_file() or stat_result.st_size <= 0:
        raise CompressionError(f"compressed output is missing, not regular, or empty: {output}")
    return stat_result.st_size


def _remove_output(output: Path) -> None:
    if output.is_symlink() or output.is_file():
        try:
            output.unlink()
        except OSError as exc:
            raise CompressionError(
                f"failed to remove stale compressed output {output}: {exc}"
            ) from exc
    elif output.exists():
        raise CompressionError(f"compressed output path has an unexpected type: {output}")


def _remove_pass_logs(prefix: Path) -> None:
    for path in prefix.parent.glob(f"{prefix.name}*"):
        if path.is_symlink() or path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                raise CompressionError(f"failed to remove FFmpeg pass log {path}: {exc}") from exc
        else:
            raise CompressionError(f"FFmpeg pass log path has an unexpected type: {path}")
