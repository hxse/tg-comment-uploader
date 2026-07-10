from __future__ import annotations

import bisect
import json
import subprocess
from collections import deque
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Literal, Mapping, Sequence, TextIO, cast


SplitStage = Literal["probing", "planning", "splitting", "validating"]


class MediaSplitError(RuntimeError):
    """A video could not be split into safe, independently usable parts."""


@dataclass(frozen=True)
class SplitPoint:
    """A legal segment boundary and the media bytes preceding it."""

    timestamp: Decimal
    cumulative_bytes: int


@dataclass(frozen=True)
class MediaProbe:
    """The byte-aware keyframe model used by the split planner."""

    points: tuple[SplitPoint, ...]
    packet_bytes: int
    duration: Decimal
    frame_rate: Fraction | None


@dataclass(frozen=True)
class SplitPlan:
    """A minimum-part, byte-balanced stream-copy segmentation plan."""

    split_times: tuple[Decimal, ...]
    estimated_part_bytes: tuple[int, ...]
    target_bytes: int
    minimized_max_part_bytes: int

    @property
    def part_count(self) -> int:
        return len(self.estimated_part_bytes)


@dataclass(frozen=True)
class SplitResult:
    """Validated files produced by one successful split attempt."""

    source: Path
    parts: tuple[Path, ...]
    plan: SplitPlan
    attempts: int


@dataclass(frozen=True)
class SplitProgress:
    """One structured update from probing, planning, splitting, or validation."""

    stage: SplitStage
    attempt: int | None = None
    max_attempts: int | None = None
    part_count: int | None = None
    processed_seconds: float | None = None
    duration_seconds: float | None = None
    fraction: float | None = None


SplitProgressCallback = Callable[[SplitProgress], None]


@dataclass(frozen=True)
class _Packet:
    timestamp: Decimal
    size: int
    stream_index: int
    keyframe: bool


class _PartsOverLimit(Exception):
    def __init__(self, sizes: tuple[int, ...]) -> None:
        super().__init__("one or more generated parts exceed the hard limit")
        self.sizes = sizes


def parse_probe_payload(payload: Mapping[str, Any]) -> MediaProbe:
    """Convert ffprobe JSON into cumulative byte positions at video keyframes."""

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaSplitError("ffprobe response does not contain a streams array")

    video_stream: Mapping[str, Any] | None = None
    for raw_stream in streams:
        if isinstance(raw_stream, Mapping) and raw_stream.get("codec_type") == "video":
            video_stream = raw_stream
            break
    if video_stream is None:
        raise MediaSplitError("source does not contain a video stream")

    video_stream_index = _strict_int(video_stream.get("index"), "video stream index")
    frame_rate = _parse_frame_rate(video_stream)

    raw_packets = payload.get("packets")
    if not isinstance(raw_packets, list) or not raw_packets:
        raise MediaSplitError("ffprobe response does not contain media packets")

    packets: list[_Packet] = []
    for packet_number, raw_packet in enumerate(raw_packets, start=1):
        if not isinstance(raw_packet, Mapping):
            raise MediaSplitError(f"ffprobe packet {packet_number} is not an object")
        packet_mapping = cast(Mapping[str, Any], raw_packet)

        timestamp = _packet_timestamp(packet_mapping, packet_number)
        size = _positive_int(packet_mapping.get("size"), f"packet {packet_number} size")
        stream_index = _strict_int(
            packet_mapping.get("stream_index"), f"packet {packet_number} stream index"
        )
        flags = packet_mapping.get("flags", "")
        if not isinstance(flags, str):
            raise MediaSplitError(f"ffprobe packet {packet_number} flags are invalid")
        packets.append(
            _Packet(
                timestamp=timestamp,
                size=size,
                stream_index=stream_index,
                keyframe="K" in flags,
            )
        )

    format_section = payload.get("format")
    if not isinstance(format_section, Mapping):
        format_section = {}
    start_time = _optional_decimal(format_section.get("start_time"))
    if start_time is None:
        start_time = min(Decimal(0), min(packet.timestamp for packet in packets))

    normalized_packets = tuple(
        _Packet(
            timestamp=packet.timestamp - start_time,
            size=packet.size,
            stream_index=packet.stream_index,
            keyframe=packet.keyframe,
        )
        for packet in packets
    )
    duration = _probe_duration(format_section, normalized_packets)

    keyframe_times = sorted(
        {
            packet.timestamp
            for packet in normalized_packets
            if packet.stream_index == video_stream_index
            and packet.keyframe
            and Decimal(0) < packet.timestamp < duration
        }
    )
    if not keyframe_times:
        raise MediaSplitError(
            "source has no internal video keyframes suitable for lossless splitting; "
            "use --oversize-policy compress"
        )

    packets_by_time = sorted(normalized_packets, key=lambda packet: packet.timestamp)
    packet_times = [packet.timestamp for packet in packets_by_time]
    prefix_bytes = [0]
    for packet in packets_by_time:
        prefix_bytes.append(prefix_bytes[-1] + packet.size)
    packet_bytes = prefix_bytes[-1]

    points: list[SplitPoint] = [SplitPoint(Decimal(0), 0)]
    for timestamp in keyframe_times:
        packet_index = bisect.bisect_left(packet_times, timestamp)
        cumulative_bytes = prefix_bytes[packet_index]
        if cumulative_bytes <= points[-1].cumulative_bytes or cumulative_bytes >= packet_bytes:
            continue
        points.append(SplitPoint(timestamp, cumulative_bytes))
    points.append(SplitPoint(duration, packet_bytes))

    if len(points) < 3:
        raise MediaSplitError(
            "source has no usable internal video keyframes for lossless splitting; "
            "use --oversize-policy compress"
        )
    return MediaProbe(
        points=tuple(points),
        packet_bytes=packet_bytes,
        duration=duration,
        frame_rate=frame_rate,
    )


def plan_split(
    probe: MediaProbe,
    *,
    source_size_bytes: int,
    target_bytes: int,
    minimum_parts: int = 2,
) -> SplitPlan:
    """Plan the fewest keyframe-aligned parts, then minimize their maximum size."""

    if source_size_bytes <= 0:
        raise ValueError("source_size_bytes must be positive")
    if target_bytes <= 0:
        raise ValueError("target_bytes must be positive")
    if minimum_parts < 2:
        raise ValueError("minimum_parts must be at least 2")

    points = probe.points
    _validate_split_points(points, probe.packet_bytes)
    available_parts = len(points) - 1
    required_by_source_size = max(
        minimum_parts,
        (source_size_bytes + target_bytes - 1) // target_bytes,
    )
    if required_by_source_size > available_parts:
        raise _keyframe_error(required_by_source_size, available_parts)

    minimum_by_keyframes = _greedy_minimum_parts(points, target_bytes)
    if minimum_by_keyframes is None:
        raise MediaSplitError(
            "at least one keyframe interval is larger than the split planning target; "
            "lossless splitting cannot guarantee the upload limit; "
            "use --oversize-policy compress"
        )

    part_count = max(required_by_source_size, minimum_by_keyframes)
    if part_count > available_parts:
        raise _keyframe_error(part_count, available_parts)

    low = max(1, (probe.packet_bytes + part_count - 1) // part_count)
    high = target_bytes
    while low < high:
        candidate = (low + high) // 2
        candidate_parts = _greedy_minimum_parts(points, candidate)
        if candidate_parts is not None and candidate_parts <= part_count:
            high = candidate
        else:
            low = candidate + 1
    minimized_max = low

    boundary_indexes = _balanced_boundaries(points, part_count, minimized_max)
    estimated_sizes = tuple(
        points[end].cumulative_bytes - points[start].cumulative_bytes
        for start, end in zip(boundary_indexes, boundary_indexes[1:])
    )
    split_times = tuple(points[index].timestamp for index in boundary_indexes[1:-1])
    return SplitPlan(
        split_times=split_times,
        estimated_part_bytes=estimated_sizes,
        target_bytes=target_bytes,
        minimized_max_part_bytes=minimized_max,
    )


def build_segment_command(
    source: Path,
    output_pattern: Path,
    plan: SplitPlan,
    *,
    ffmpeg_binary: str = "ffmpeg",
    frame_rate: Fraction | None = None,
) -> list[str]:
    """Build one shell-free FFmpeg segment-muxer invocation."""

    if not plan.split_times:
        raise ValueError("a split command requires at least one split time")

    command = [
        ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-reference_stream",
        "v:0",
        "-segment_start_number",
        "1",
        "-segment_times",
        ",".join(_format_decimal(timestamp) for timestamp in plan.split_times),
    ]
    if frame_rate is not None and frame_rate > 0:
        delta = Fraction(frame_rate.denominator, frame_rate.numerator * 2)
        command.extend(["-segment_time_delta", _format_fraction(delta)])
    command.extend(
        [
            "-reset_timestamps",
            "1",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_pattern),
        ]
    )
    return command


def split_video(
    source: Path,
    work_dir: Path,
    *,
    hard_limit_bytes: int,
    target_bytes: int,
    progress: SplitProgressCallback | None = None,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
    max_attempts: int = 3,
) -> SplitResult:
    """Stream-copy an input into validated parts below one shared hard limit."""

    _validate_split_request(
        source,
        work_dir,
        hard_limit_bytes=hard_limit_bytes,
        target_bytes=target_bytes,
        max_attempts=max_attempts,
    )
    source_size = source.stat().st_size
    try:
        work_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as exc:
        raise MediaSplitError(f"failed to create split work directory {work_dir}: {exc}") from exc
    if work_dir.is_symlink() or not work_dir.is_dir():
        raise MediaSplitError(f"split work directory is not a regular directory: {work_dir}")
    try:
        work_dir.chmod(0o700)
    except OSError as exc:
        raise MediaSplitError(f"failed to secure split work directory {work_dir}: {exc}") from exc
    _clear_part_outputs(work_dir, source)
    _emit_progress(progress, SplitProgress(stage="probing"))
    probe = probe_media(source, ffprobe_binary=ffprobe_binary)
    duration_seconds = float(probe.duration)

    planning_target = target_bytes
    for attempt in range(1, max_attempts + 1):
        plan = plan_split(
            probe,
            source_size_bytes=source_size,
            target_bytes=planning_target,
        )
        _emit_progress(
            progress,
            SplitProgress(
                stage="planning",
                attempt=attempt,
                max_attempts=max_attempts,
                part_count=plan.part_count,
                duration_seconds=duration_seconds,
            ),
        )
        _clear_part_outputs(work_dir, source)
        output_pattern = _part_output_pattern(work_dir, source)
        command = build_segment_command(
            source,
            output_pattern,
            plan,
            ffmpeg_binary=ffmpeg_binary,
            frame_rate=probe.frame_rate,
        )
        try:
            completed = _run_ffmpeg(
                command,
                duration=probe.duration,
                attempt=attempt,
                max_attempts=max_attempts,
                part_count=plan.part_count,
                progress=progress,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or "no diagnostic output"
                raise MediaSplitError(f"FFmpeg lossless split failed: {detail}")

            parts = _expected_part_paths(work_dir, source, plan.part_count)
            try:
                _emit_progress(
                    progress,
                    SplitProgress(
                        stage="validating",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        part_count=plan.part_count,
                        duration_seconds=duration_seconds,
                    ),
                )
                _validate_generated_parts(
                    parts,
                    source=source,
                    work_dir=work_dir,
                    hard_limit_bytes=hard_limit_bytes,
                    ffprobe_binary=ffprobe_binary,
                )
            except _PartsOverLimit as exc:
                _clear_part_outputs(work_dir, source)
                if attempt >= max_attempts:
                    largest = max(exc.sizes)
                    raise MediaSplitError(
                        "lossless split still produced an oversized part after "
                        f"{max_attempts} attempts; largest={largest:,} bytes, "
                        f"limit={hard_limit_bytes:,} bytes; use --oversize-policy compress"
                    ) from exc
                planning_target = _tighten_target(
                    current_target=planning_target,
                    hard_limit_bytes=hard_limit_bytes,
                    actual_sizes=exc.sizes,
                    estimated_sizes=plan.estimated_part_bytes,
                )
                continue
        except BaseException:
            _clear_part_outputs(work_dir, source)
            raise

        return SplitResult(source=source, parts=parts, plan=plan, attempts=attempt)

    raise AssertionError("split attempt loop exited unexpectedly")


def probe_media(source: Path, *, ffprobe_binary: str = "ffprobe") -> MediaProbe:
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_packets",
        "-show_entries",
        (
            "format=start_time,duration:"
            "stream=index,codec_type,avg_frame_rate,r_frame_rate:"
            "packet=stream_index,pts_time,dts_time,duration_time,size,flags"
        ),
        str(source),
    ]
    completed = _run_command(command)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no diagnostic output"
        raise MediaSplitError(f"ffprobe failed for {source}: {detail}")
    payload = _parse_json_object(completed.stdout, context=f"ffprobe output for {source}")
    return parse_probe_payload(payload)


def _validate_split_request(
    source: Path,
    work_dir: Path,
    *,
    hard_limit_bytes: int,
    target_bytes: int,
    max_attempts: int,
) -> None:
    if not source.is_absolute():
        raise MediaSplitError(f"split source path must be absolute: {source}")
    if not source.exists() or not source.is_file():
        raise MediaSplitError(f"split source is not a regular file: {source}")
    if source.stat().st_size <= 0:
        raise MediaSplitError(f"split source is empty: {source}")
    if not work_dir.is_absolute():
        raise MediaSplitError(f"split work directory must be absolute: {work_dir}")
    if hard_limit_bytes <= 0:
        raise ValueError("hard_limit_bytes must be positive")
    if target_bytes <= 0 or target_bytes > hard_limit_bytes:
        raise ValueError("target_bytes must be positive and no greater than hard_limit_bytes")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")

    source_resolved = source.resolve()
    work_resolved = work_dir.resolve(strict=False)
    if source_resolved == work_resolved or source_resolved.is_relative_to(work_resolved):
        raise MediaSplitError("split source must not be located inside the work directory")
    if work_dir.is_symlink():
        raise MediaSplitError(f"split work directory must not be a symbolic link: {work_dir}")
    if work_dir.exists() and not work_dir.is_dir():
        raise MediaSplitError(f"split work path is not a directory: {work_dir}")


def _validate_split_points(points: Sequence[SplitPoint], packet_bytes: int) -> None:
    if len(points) < 3:
        raise MediaSplitError("at least one internal keyframe is required for splitting")
    if packet_bytes <= 0:
        raise MediaSplitError("probed media contains no packet bytes")
    if points[0].cumulative_bytes != 0:
        raise MediaSplitError("first split point must start at zero bytes")
    if points[-1].cumulative_bytes != packet_bytes:
        raise MediaSplitError("last split point must equal the probed packet byte total")
    for previous, current in zip(points, points[1:]):
        if current.timestamp <= previous.timestamp:
            raise MediaSplitError("split point timestamps must be strictly increasing")
        if current.cumulative_bytes <= previous.cumulative_bytes:
            raise MediaSplitError("split point byte positions must be strictly increasing")


def _greedy_minimum_parts(points: Sequence[SplitPoint], max_bytes: int) -> int | None:
    cumulative = [point.cumulative_bytes for point in points]
    end = len(points) - 1
    current = 0
    count = 0
    while current < end:
        next_index = (
            bisect.bisect_right(
                cumulative,
                cumulative[current] + max_bytes,
                lo=current + 1,
                hi=end + 1,
            )
            - 1
        )
        if next_index <= current:
            return None
        current = next_index
        count += 1
    return count


def _balanced_boundaries(
    points: Sequence[SplitPoint],
    part_count: int,
    max_bytes: int,
) -> tuple[int, ...]:
    cumulative = [point.cumulative_bytes for point in points]
    end = len(points) - 1
    min_parts_from: list[int | None] = [None] * len(points)
    min_parts_from[end] = 0
    for index in range(end - 1, -1, -1):
        farthest = (
            bisect.bisect_right(
                cumulative,
                cumulative[index] + max_bytes,
                lo=index + 1,
                hi=end + 1,
            )
            - 1
        )
        farthest_minimum = min_parts_from[farthest]
        if farthest > index and farthest_minimum is not None:
            min_parts_from[index] = 1 + farthest_minimum

    boundaries = [0]
    current = 0
    for part_number in range(part_count - 1):
        remaining_parts = part_count - part_number
        remaining_bytes = cumulative[end] - cumulative[current]
        best_index: int | None = None
        best_score: tuple[int, int] | None = None
        latest_index = end - (remaining_parts - 1)
        for candidate in range(current + 1, latest_index + 1):
            part_size = cumulative[candidate] - cumulative[current]
            if part_size > max_bytes:
                break
            suffix_minimum = min_parts_from[candidate]
            if suffix_minimum is None or suffix_minimum > remaining_parts - 1:
                continue
            # Compare to the exact remaining average without introducing floats.
            deviation = abs(part_size * remaining_parts - remaining_bytes)
            global_deviation = abs(
                cumulative[candidate] * part_count - cumulative[end] * (part_number + 1)
            )
            score = (deviation, global_deviation)
            if best_score is None or score < best_score:
                best_index = candidate
                best_score = score
        if best_index is None:
            raise MediaSplitError("failed to construct a feasible balanced split plan")
        boundaries.append(best_index)
        current = best_index
    boundaries.append(end)
    return tuple(boundaries)


def _keyframe_error(required_parts: int, available_parts: int) -> MediaSplitError:
    return MediaSplitError(
        f"lossless splitting needs at least {required_parts} parts but the source has "
        f"only {available_parts} keyframe-aligned intervals; "
        "use --oversize-policy compress"
    )


def _probe_duration(
    format_section: Mapping[str, Any],
    packets: Sequence[_Packet],
) -> Decimal:
    duration = _optional_decimal(format_section.get("duration"))
    packet_end = max(packet.timestamp for packet in packets)
    if duration is None or duration <= packet_end:
        duration = packet_end + Decimal("0.000001")
    if duration <= 0:
        raise MediaSplitError("source duration is not positive")
    return duration


def _packet_timestamp(packet: Mapping[str, Any], packet_number: int) -> Decimal:
    for key in ("pts_time", "dts_time"):
        timestamp = _optional_decimal(packet.get(key))
        if timestamp is not None:
            return timestamp
    raise MediaSplitError(f"ffprobe packet {packet_number} has no usable timestamp")


def _parse_frame_rate(stream: Mapping[str, Any]) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not isinstance(raw, str):
            continue
        try:
            rate = Fraction(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return rate
    return None


def _optional_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    if not result.is_finite():
        return None
    return result


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise MediaSplitError(f"ffprobe {label} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MediaSplitError(f"ffprobe {label} is invalid") from exc
    if isinstance(value, float) and not value.is_integer():
        raise MediaSplitError(f"ffprobe {label} is invalid")
    return result


def _positive_int(value: Any, label: str) -> int:
    result = _strict_int(value, label)
    if result <= 0:
        raise MediaSplitError(f"ffprobe {label} must be positive")
    return result


def _parse_json_object(value: str, *, context: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MediaSplitError(f"{context} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MediaSplitError(f"{context} is not a JSON object")
    return payload


def parse_ffmpeg_progress(
    fields: Mapping[str, str],
    duration: Decimal,
) -> tuple[float | None, float | None]:
    """Return processed seconds and a clamped fraction for one progress block."""

    processed_us = _progress_time_us(fields)
    if processed_us is None:
        return None, 1.0 if fields.get("progress") == "end" else None

    processed_seconds = max(processed_us, 0) / 1_000_000
    duration_us = int((duration * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if duration_us <= 0:
        return processed_seconds, None
    fraction = min(max(processed_us / duration_us, 0.0), 1.0)
    if fields.get("progress") == "end":
        fraction = 1.0
    return processed_seconds, fraction


def _progress_time_us(fields: Mapping[str, str]) -> int | None:
    for key in ("out_time_us", "out_time_ms"):
        value = fields.get(key)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                pass

    value = fields.get("out_time")
    if value is None:
        return None
    try:
        hours_text, minutes_text, seconds_text = value.split(":", maxsplit=2)
        seconds = Decimal(hours_text) * 3600 + Decimal(minutes_text) * 60 + Decimal(seconds_text)
    except (InvalidOperation, ValueError):
        return None
    return int((seconds * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _run_ffmpeg(
    command: list[str],
    *,
    duration: Decimal,
    attempt: int,
    max_attempts: int,
    part_count: int,
    progress: SplitProgressCallback | None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise MediaSplitError(
            f"required media tool {command[0]!r} was not found; run `just dev-shell` and retry"
        ) from exc
    except OSError as exc:
        raise MediaSplitError(f"failed to execute {command[0]!r}: {exc}") from exc

    stderr_lines: deque[str] = deque(maxlen=200)
    stderr_errors: list[BaseException] = []
    stderr_thread: Thread | None = None
    try:
        if process.stdout is None or process.stderr is None:
            raise MediaSplitError("FFmpeg output pipes were not created")
        stderr_thread = Thread(
            target=_drain_stderr,
            args=(process.stderr, stderr_lines, stderr_errors),
            name="tg-comment-uploader-ffmpeg-stderr",
            daemon=True,
        )
        stderr_thread.start()

        duration_seconds = float(duration)
        _emit_progress(
            progress,
            SplitProgress(
                stage="splitting",
                attempt=attempt,
                max_attempts=max_attempts,
                part_count=part_count,
                processed_seconds=0.0,
                duration_seconds=duration_seconds,
                fraction=0.0,
            ),
        )
        fields: dict[str, str] = {}
        final_event: SplitProgress | None = None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            key, separator, value = line.partition("=")
            if separator and key.replace("_", "").isalnum():
                fields[key] = value
            if key == "progress" and separator:
                processed_seconds, fraction = parse_ffmpeg_progress(fields, duration)
                event = SplitProgress(
                    stage="splitting",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    part_count=part_count,
                    processed_seconds=processed_seconds,
                    duration_seconds=duration_seconds,
                    fraction=fraction,
                )
                if value == "end":
                    final_event = event
                else:
                    _emit_progress(progress, event)
                fields = {}
        return_code = process.wait()
    except BaseException:
        try:
            _terminate_process(process)
        finally:
            if stderr_thread is not None:
                stderr_thread.join()
        raise

    if stderr_thread is not None:
        stderr_thread.join()
    if stderr_errors:
        error = stderr_errors[0]
        raise MediaSplitError(f"failed to read FFmpeg diagnostics: {error}") from error
    if return_code == 0:
        if final_event is None:
            final_event = SplitProgress(
                stage="splitting",
                attempt=attempt,
                max_attempts=max_attempts,
                part_count=part_count,
                processed_seconds=float(duration),
                duration_seconds=float(duration),
                fraction=1.0,
            )
        _emit_progress(progress, final_event)

    return subprocess.CompletedProcess(
        command,
        return_code,
        "",
        "\n".join(stderr_lines).strip(),
    )


def _drain_stderr(
    stream: TextIO,
    lines: deque[str],
    errors: list[BaseException],
) -> None:
    try:
        for raw_line in stream:
            lines.append(raw_line.rstrip("\r\n"))
    except BaseException as exc:
        errors.append(exc)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _emit_progress(
    callback: SplitProgressCallback | None,
    event: SplitProgress,
) -> None:
    if callback is not None:
        callback(event)


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise MediaSplitError(
            f"required media tool {command[0]!r} was not found; run `just dev-shell` and retry"
        ) from exc
    except OSError as exc:
        raise MediaSplitError(f"failed to execute {command[0]!r}: {exc}") from exc


def _part_output_pattern(work_dir: Path, source: Path) -> Path:
    prefix = f"{source.stem} part-".replace("%", "%%")
    suffix = source.suffix.replace("%", "%%")
    return work_dir / f"{prefix}%04d{suffix}"


def _part_path(work_dir: Path, source: Path, index: int) -> Path:
    return work_dir / f"{source.stem} part-{index:04d}{source.suffix}"


def _expected_part_paths(
    work_dir: Path,
    source: Path,
    part_count: int,
) -> tuple[Path, ...]:
    return tuple(_part_path(work_dir, source, index) for index in range(1, part_count + 1))


def _numbered_part_paths(work_dir: Path, source: Path) -> tuple[Path, ...]:
    if not work_dir.exists():
        return ()

    prefix = f"{source.stem} part-"
    suffix = source.suffix
    numbered: list[tuple[int, Path]] = []
    for path in work_dir.iterdir():
        name = path.name
        if not name.startswith(prefix) or (suffix and not name.endswith(suffix)):
            continue
        end = len(name) - len(suffix) if suffix else len(name)
        number = name[len(prefix) : end]
        if not number.isascii() or not number.isdecimal():
            continue
        numbered.append((int(number), path))
    numbered.sort(key=lambda item: item[0])
    return tuple(path for _, path in numbered)


def _validate_generated_parts(
    parts: tuple[Path, ...],
    *,
    source: Path,
    work_dir: Path,
    hard_limit_bytes: int,
    ffprobe_binary: str,
) -> None:
    if len(parts) < 2:
        raise MediaSplitError("lossless splitting must generate at least two parts")

    actual_paths = _numbered_part_paths(work_dir, source)
    if actual_paths != parts:
        expected_names = ", ".join(path.name for path in parts)
        actual_names = ", ".join(path.name for path in actual_paths) or "<none>"
        raise MediaSplitError(
            "FFmpeg generated an unexpected part sequence; "
            f"expected=[{expected_names}], actual=[{actual_names}]"
        )

    sizes: list[int] = []
    for part in parts:
        if part.is_symlink() or not part.is_file():
            raise MediaSplitError(f"generated part is not a regular file: {part}")
        try:
            part.chmod(0o600)
            size = part.stat().st_size
        except OSError as exc:
            raise MediaSplitError(f"failed to secure generated part {part}: {exc}") from exc
        if size <= 0:
            raise MediaSplitError(f"generated part is empty: {part}")
        sizes.append(size)

    if any(size > hard_limit_bytes for size in sizes):
        raise _PartsOverLimit(tuple(sizes))

    for part in parts:
        _validate_part_probe(part, ffprobe_binary=ffprobe_binary)


def _validate_part_probe(part: Path, *, ffprobe_binary: str) -> None:
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_entries",
        "stream=index,codec_type",
        str(part),
    ]
    completed = _run_command(command)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no diagnostic output"
        raise MediaSplitError(f"generated part cannot be probed: {part}: {detail}")
    payload = _parse_json_object(completed.stdout, context=f"ffprobe output for {part}")
    streams = payload.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, Mapping) and stream.get("codec_type") == "video" for stream in streams
    ):
        raise MediaSplitError(f"generated part does not contain a usable video stream: {part}")


def _tighten_target(
    *,
    current_target: int,
    hard_limit_bytes: int,
    actual_sizes: tuple[int, ...],
    estimated_sizes: tuple[int, ...],
) -> int:
    if len(actual_sizes) != len(estimated_sizes):
        raise MediaSplitError("cannot adjust split target for an unexpected part count")
    worst_overhead = max(
        max(0, actual - estimated)
        for actual, estimated in zip(actual_sizes, estimated_sizes, strict=True)
    )
    largest_actual = max(actual_sizes)
    cushion = max(1, hard_limit_bytes // 200)
    overhead_based = hard_limit_bytes - worst_overhead - cushion
    ratio_based = current_target * max(1, hard_limit_bytes - cushion) // largest_actual
    tightened = min(current_target - 1, overhead_based, ratio_based)
    if tightened <= 0:
        raise MediaSplitError(
            "container overhead leaves no usable target for lossless splitting; "
            "use --oversize-policy compress"
        )
    return tightened


def _clear_part_outputs(work_dir: Path, source: Path) -> None:
    for path in _numbered_part_paths(work_dir, source):
        if path.is_dir() and not path.is_symlink():
            raise MediaSplitError(f"refuse to remove unexpected part directory: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise MediaSplitError(f"failed to remove stale split output {path}: {exc}") from exc


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _format_fraction(value: Fraction) -> str:
    return _format_decimal(Decimal(value.numerator) / Decimal(value.denominator))
