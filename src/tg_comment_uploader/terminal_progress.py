"""Small terminal-aware progress renderer shared by CLI workflows."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from threading import Event, Lock, Thread
from types import TracebackType
from typing import Self, TextIO

BAR_WIDTH = 20
NON_TTY_REPORT_INTERVAL_SECONDS = 10.0
RESPONSE_WAIT_MESSAGE = "request sent to local Bot API; waiting for Telegram response"
TTY_RESPONSE_WAIT_INTERVAL_SECONDS = 1.0
NON_TTY_RESPONSE_WAIT_INTERVAL_SECONDS = 30.0
SPINNER_FRAMES = ("|", "/", "-", "\\")

Clock = Callable[[], float]


class TerminalProgress:
    """Render one active progress phase without corrupting ordinary output."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock if clock is not None else time.monotonic
        try:
            self._is_tty = bool(self._stream.isatty())
        except Exception:
            self._is_tty = False

        self._label: str | None = None
        self._started_at = 0.0
        self._last_report_at: float | None = None
        self._last_reported_fraction: float | None = None
        self._rendered_width = 0
        self._line_open = False
        self._completed = False

    def update(self, label: str, fraction: float) -> None:
        """Update ``label`` with a completion fraction clamped to ``[0, 1]``."""

        normalized_label = _normalize_label(label)
        normalized_fraction = _clamp_fraction(fraction)
        now = self._clock()

        if self._label != normalized_label or (self._completed and normalized_fraction < 1.0):
            self._finish_line()
            self._begin_phase(normalized_label, now)
        elif self._completed:
            return

        elapsed = max(now - self._started_at, 0.0)
        should_report = self._is_tty or self._should_report_non_tty(
            normalized_fraction,
            now,
        )
        if should_report:
            line = _format_progress_line(normalized_label, normalized_fraction, elapsed)
            if self._is_tty:
                self._render_tty(line)
            else:
                self._stream.write(f"{line}\n")
                self._stream.flush()
            self._last_report_at = now
            self._last_reported_fraction = normalized_fraction

        if normalized_fraction >= 1.0:
            if self._is_tty:
                self._finish_line()
            self._completed = True

    def log(self, message: str) -> None:
        """Write a normal line, first moving past any active TTY progress line."""

        self._finish_line()
        self._stream.write(f"{message}\n")
        self._stream.flush()

    def finish(self) -> None:
        """Finish any active line and reset the renderer; safe to call repeatedly."""

        self._finish_line()
        self._reset_phase()
        self._stream.flush()

    def _begin_phase(self, label: str, now: float) -> None:
        self._label = label
        self._started_at = now
        self._last_report_at = None
        self._last_reported_fraction = None
        self._completed = False

    def _should_report_non_tty(self, fraction: float, now: float) -> bool:
        if self._last_report_at is None:
            return True
        if fraction >= 1.0 and self._last_reported_fraction != 1.0:
            return True
        return now - self._last_report_at >= NON_TTY_REPORT_INTERVAL_SECONDS

    def _render_tty(self, line: str) -> None:
        padding = " " * max(self._rendered_width - len(line), 0)
        self._stream.write(f"\r{line}{padding}")
        self._stream.flush()
        self._rendered_width = len(line)
        self._line_open = True

    def _finish_line(self) -> None:
        if self._is_tty and self._line_open:
            self._stream.write("\n")
            self._stream.flush()
        self._rendered_width = 0
        self._line_open = False

    def _reset_phase(self) -> None:
        self._label = None
        self._started_at = 0.0
        self._last_report_at = None
        self._last_reported_fraction = None
        self._completed = False


def _normalize_label(label: str) -> str:
    if not isinstance(label, str):
        raise TypeError("progress label must be a string")
    normalized = " ".join(label.splitlines()).strip()
    if not normalized:
        raise ValueError("progress label must not be empty")
    return normalized


def _clamp_fraction(fraction: float) -> float:
    value = float(fraction)
    if math.isnan(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def _format_progress_line(label: str, fraction: float, elapsed: float) -> str:
    completed_cells = min(int(fraction * BAR_WIDTH), BAR_WIDTH)
    bar = "#" * completed_cells + "-" * (BAR_WIDTH - completed_cells)
    percent = fraction * 100
    eta = _estimate_eta(fraction, elapsed)
    eta_text = "--:--" if eta is None else _format_duration(eta)
    return f"{label} [{bar}] {percent:6.1f}% elapsed {_format_duration(elapsed)} eta {eta_text}"


def _estimate_eta(fraction: float, elapsed: float) -> float | None:
    if fraction >= 1.0:
        return 0.0
    if fraction <= 0.0 or elapsed <= 0.0:
        return None
    return elapsed * (1.0 - fraction) / fraction


def _format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


class ResponseWaitIndicator:
    """Show elapsed time while a sent Bot API request awaits its response."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        clock: Clock | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock if clock is not None else time.monotonic
        try:
            self._is_tty = bool(self._stream.isatty())
        except Exception:
            self._is_tty = False

        default_interval = (
            TTY_RESPONSE_WAIT_INTERVAL_SECONDS
            if self._is_tty
            else NON_TTY_RESPONSE_WAIT_INTERVAL_SECONDS
        )
        self._interval_seconds = (
            default_interval if interval_seconds is None else float(interval_seconds)
        )
        if not math.isfinite(self._interval_seconds) or self._interval_seconds <= 0:
            raise ValueError("response wait interval must be finite and positive")

        self._stop_event = Event()
        self._write_lock = Lock()
        self._thread: Thread | None = None
        self._started_at = 0.0
        self._frame_index = 0
        self._rendered_width = 0
        self._line_open = False
        self._disabled = False

    @property
    def running(self) -> bool:
        """Whether the indicator currently owns a live worker thread."""

        thread = self._thread
        return thread is not None and thread.is_alive()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.stop()

    def start(self) -> None:
        """Render immediately and start periodic TTY frames or log heartbeats."""

        if self._thread is not None:
            return
        self._stop_event.clear()
        self._frame_index = 0
        self._rendered_width = 0
        self._line_open = False
        self._disabled = False
        try:
            self._started_at = self._clock()
            self._render(self._started_at)
        except Exception:
            self._disabled = True
            self._safe_finish_line()
            return

        try:
            thread = Thread(
                target=self._run,
                name="tg-comment-uploader-response-wait",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except Exception:
            self._stop_event.set()
            if self._thread is not None:
                try:
                    self._thread.join()
                except Exception:
                    pass
            self._thread = None
            self._disabled = True
            self._safe_finish_line()
        except BaseException:
            self._stop_event.set()
            if self._thread is not None:
                try:
                    self._thread.join()
                except Exception:
                    pass
            self._thread = None
            self._safe_finish_line()
            raise

    def stop(self) -> None:
        """Stop and join the worker, then terminate an active TTY line."""

        thread = self._thread
        if thread is None:
            self._safe_finish_line()
            return
        self._stop_event.set()
        try:
            thread.join()
        except Exception:
            # The stop event is already set and the worker is a daemon, so a
            # reporting failure must not alter the HTTP request outcome.
            pass
        finally:
            self._thread = None
            self._safe_finish_line()

    def _run(self) -> None:
        while True:
            try:
                if self._stop_event.wait(self._interval_seconds):
                    return
                self._render(self._clock())
            except Exception:
                self._disabled = True
                return

    def _render(self, now: float) -> None:
        if self._disabled:
            return
        elapsed = max(now - self._started_at, 0.0)
        elapsed_text = _format_duration(elapsed)
        with self._write_lock:
            if self._is_tty:
                frame = SPINNER_FRAMES[self._frame_index % len(SPINNER_FRAMES)]
                line = f"{frame} {RESPONSE_WAIT_MESSAGE} (elapsed {elapsed_text})"
                padding = " " * max(self._rendered_width - len(line), 0)
                self._stream.write(f"\r{line}{padding}")
                self._rendered_width = len(line)
                self._line_open = True
                self._frame_index += 1
            else:
                self._stream.write(f"{RESPONSE_WAIT_MESSAGE} (elapsed {elapsed_text})\n")
            self._stream.flush()

    def _safe_finish_line(self) -> None:
        try:
            self._finish_line()
        except Exception:
            self._disabled = True

    def _finish_line(self) -> None:
        with self._write_lock:
            if self._is_tty and self._line_open:
                self._stream.write("\n")
                self._stream.flush()
            self._rendered_width = 0
            self._line_open = False
