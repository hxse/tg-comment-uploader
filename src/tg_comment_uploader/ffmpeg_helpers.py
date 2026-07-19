from __future__ import annotations

import subprocess
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Callable, Mapping, TypeVar


ProgressEvent = TypeVar("ProgressEvent")


def progress_time_us(fields: Mapping[str, str]) -> int | None:
    """Extract FFmpeg's processed timestamp from one progress block."""

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


def parse_ffmpeg_progress(
    fields: Mapping[str, str],
    duration_us: int,
) -> tuple[float | None, float | None]:
    """Return processed seconds and a clamped fraction for one progress block."""

    processed_us = progress_time_us(fields)
    if processed_us is None:
        return None, 1.0 if fields.get("progress") == "end" else None

    processed_seconds = max(processed_us, 0) / 1_000_000
    if duration_us <= 0:
        return processed_seconds, None
    fraction = min(max(processed_us / duration_us, 0.0), 1.0)
    if fields.get("progress") == "end":
        fraction = 1.0
    return processed_seconds, fraction


def is_attached_picture(stream: Mapping[str, object]) -> bool:
    """Return whether an ffprobe video stream is an embedded cover image."""

    disposition = stream.get("disposition")
    return isinstance(disposition, Mapping) and disposition.get("attached_pic") == 1


def terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate an FFmpeg process, escalating to kill after a short grace period."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def emit_progress(
    callback: Callable[[ProgressEvent], None] | None,
    event: ProgressEvent,
) -> None:
    """Deliver a structured progress event when a callback was supplied."""

    if callback is not None:
        callback(event)
