from __future__ import annotations

import io
import math
import time
from collections.abc import Callable

import pytest

from tg_comment_uploader.terminal_progress import (
    BAR_WIDTH,
    TELEGRAM_CONFIRMATION_WAIT_MESSAGE,
    TelegramConfirmationWaitIndicator,
    TerminalProgress,
)


class FakeStream(io.StringIO):
    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_tty_renders_fixed_width_bar_elapsed_eta_and_finishes_completion() -> None:
    stream = FakeStream(is_tty=True)
    clock = FakeClock(100.0)
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("compress", 0.0)
    clock.now = 110.0
    progress.update("compress", 0.5)
    clock.now = 120.0
    progress.update("compress", 1.0)
    progress.finish()

    output = stream.getvalue()
    assert f"[{'-' * BAR_WIDTH}]" in output
    assert f"[{'#' * 10}{'-' * 10}]" in output
    assert f"[{'#' * BAR_WIDTH}]" in output
    assert "   0.0% elapsed 0:00 eta --:--" in output
    assert "  50.0% elapsed 0:10 eta 0:10" in output
    assert " 100.0% elapsed 0:20 eta 0:00" in output
    assert output.count("\r") == 3
    assert output.count("\n") == 1
    assert output.endswith("\n")


def test_non_tty_outputs_start_every_ten_seconds_and_completion_as_lines() -> None:
    stream = FakeStream(is_tty=False)
    clock = FakeClock()
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("upload", 0.0)
    clock.now = 9.9
    progress.update("upload", 0.2)
    clock.now = 10.0
    progress.update("upload", 0.5)
    clock.now = 10.1
    progress.update("upload", 1.0)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert "0.0%" in lines[0]
    assert "50.0%" in lines[1]
    assert "100.0%" in lines[2]
    assert "\r" not in stream.getvalue()


def test_non_tty_finish_does_not_add_a_blank_line() -> None:
    stream = FakeStream(is_tty=False)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("upload", 0.0)
    progress.finish()
    progress.finish()

    assert stream.getvalue().count("\n") == 1


def test_label_switch_finishes_old_tty_line_and_starts_a_new_timer() -> None:
    stream = FakeStream(is_tty=True)
    clock = FakeClock()
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("pass 1", 0.5)
    clock.now = 12.0
    progress.update("pass 2", 0.5)
    progress.finish()

    output = stream.getvalue()
    assert output.count("\n") == 2
    assert "pass 1" in output
    assert "pass 2" in output
    assert "pass 2" in output and "elapsed 0:00 eta --:--" in output


def test_log_moves_past_tty_progress_and_phase_can_continue() -> None:
    stream = FakeStream(is_tty=True)
    clock = FakeClock()
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("upload", 0.25)
    progress.log("retrying shortly")
    clock.now = 10.0
    progress.update("upload", 0.5)
    progress.finish()

    output = stream.getvalue()
    assert "eta --:--\nretrying shortly\n\rupload" in output
    assert "elapsed 0:10 eta 0:10" in output
    assert output.endswith("\n")


def test_log_is_an_ordinary_line_in_non_tty_mode() -> None:
    stream = FakeStream(is_tty=False)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("upload", 0.0)
    progress.log("waiting for Telegram")

    output = stream.getvalue()
    assert output.splitlines()[-1] == "waiting for Telegram"
    assert "\r" not in output


def test_finish_terminates_an_incomplete_tty_line_and_is_idempotent() -> None:
    stream = FakeStream(is_tty=True)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("upload", 0.4)
    progress.finish()
    progress.finish()

    assert stream.getvalue().endswith("\n")
    assert stream.getvalue().count("\n") == 1


def test_tty_update_pads_a_shorter_line_to_clear_old_content() -> None:
    stream = FakeStream(is_tty=True)
    clock = FakeClock()
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("encode", 0.0)
    clock.now = 1.0
    progress.update("encode", 0.0001)
    clock.now = 2.0
    progress.update("encode", 0.9)

    last_update = stream.getvalue().split("\r")[-1]
    assert "eta 0:00" in last_update
    assert last_update.endswith("   ")


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (-1.0, "0.0%"),
        (2.0, "100.0%"),
        (-math.inf, "0.0%"),
        (math.inf, "100.0%"),
        (math.nan, "0.0%"),
    ],
)
def test_fraction_is_clamped_and_nan_is_safe(fraction: float, expected: str) -> None:
    stream = FakeStream(is_tty=False)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("work", fraction)

    assert expected in stream.getvalue()


def test_repeated_completion_and_finish_do_not_duplicate_output() -> None:
    stream = FakeStream(is_tty=True)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("upload", 1.0)
    progress.update("upload", 1.0)
    progress.finish()

    assert stream.getvalue().count("100.0%") == 1
    assert stream.getvalue().count("\n") == 1


def test_same_label_can_start_again_after_completed_fraction_decreases() -> None:
    stream = FakeStream(is_tty=False)
    clock = FakeClock()
    progress = TerminalProgress(stream=stream, clock=clock)

    progress.update("upload", 1.0)
    clock.now = 5.0
    progress.update("upload", 0.0)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert "100.0%" in lines[0]
    assert "0.0% elapsed 0:00 eta --:--" in lines[1]


@pytest.mark.parametrize("label", ["", "   ", "\n"])
def test_empty_label_is_rejected(label: str) -> None:
    progress = TerminalProgress(stream=FakeStream(is_tty=False), clock=FakeClock())

    with pytest.raises(ValueError, match="must not be empty"):
        progress.update(label, 0.0)


def test_multiline_label_is_normalized_to_one_terminal_line() -> None:
    stream = FakeStream(is_tty=False)
    progress = TerminalProgress(stream=stream, clock=FakeClock())

    progress.update("first\nsecond", 0.0)

    assert stream.getvalue().startswith("first second [")
    assert len(stream.getvalue().splitlines()) == 1


class StepClock:
    def __init__(self, *, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current


def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    pytest.fail("timed out waiting for Telegram confirmation indicator update")


def test_telegram_confirmation_wait_tty_spins_on_one_line_and_finishes_with_newline() -> None:
    stream = FakeStream(is_tty=True)
    indicator = TelegramConfirmationWaitIndicator(
        stream=stream,
        clock=StepClock(step=1.0),
        interval_seconds=0.01,
    )

    indicator.start()
    assert indicator.running is True
    wait_until(lambda: stream.getvalue().count("\r") >= 2)
    indicator.stop()

    output = stream.getvalue()
    assert indicator.running is False
    assert TELEGRAM_CONFIRMATION_WAIT_MESSAGE in output
    assert "(elapsed 0:00)" in output
    assert "(elapsed 0:01)" in output
    assert output.count("\r") >= 2
    assert output.count("\n") == 1
    assert output.endswith("\n")
    assert "%" not in output
    assert " eta " not in output


def test_telegram_confirmation_wait_non_tty_logs_immediately_and_heartbeats_without_carriage() -> (
    None
):
    stream = FakeStream(is_tty=False)
    indicator = TelegramConfirmationWaitIndicator(
        stream=stream,
        clock=StepClock(step=30.0),
        interval_seconds=0.01,
    )

    indicator.start()
    wait_until(lambda: len(stream.getvalue().splitlines()) >= 2)
    indicator.stop()

    lines = stream.getvalue().splitlines()
    assert lines[0] == f"{TELEGRAM_CONFIRMATION_WAIT_MESSAGE} (elapsed 0:00)"
    assert lines[1] == f"{TELEGRAM_CONFIRMATION_WAIT_MESSAGE} (elapsed 0:30)"
    assert "\r" not in stream.getvalue()
    assert all("%" not in line and " eta " not in line for line in lines)


def test_telegram_confirmation_wait_context_stops_thread_on_keyboard_interrupt() -> None:
    stream = FakeStream(is_tty=True)
    indicator = TelegramConfirmationWaitIndicator(stream=stream, interval_seconds=60.0)

    with pytest.raises(KeyboardInterrupt):
        with indicator:
            assert indicator.running is True
            raise KeyboardInterrupt

    assert indicator.running is False
    assert stream.getvalue().endswith("\n")


def test_telegram_confirmation_wait_start_and_stop_are_idempotent() -> None:
    stream = FakeStream(is_tty=True)
    indicator = TelegramConfirmationWaitIndicator(stream=stream, interval_seconds=60.0)

    indicator.start()
    indicator.start()
    indicator.stop()
    indicator.stop()

    assert stream.getvalue().count(TELEGRAM_CONFIRMATION_WAIT_MESSAGE) == 1
    assert stream.getvalue().count("\n") == 1
    assert indicator.running is False


@pytest.mark.parametrize("interval", [0.0, -1.0, math.inf, math.nan])
def test_telegram_confirmation_wait_rejects_invalid_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        TelegramConfirmationWaitIndicator(
            stream=FakeStream(is_tty=False),
            interval_seconds=interval,
        )


class BrokenOutputStream(FakeStream):
    def write(self, value: str) -> int:
        raise RuntimeError("output unavailable")


def test_telegram_confirmation_wait_output_failure_is_silently_disabled() -> None:
    indicator = TelegramConfirmationWaitIndicator(
        stream=BrokenOutputStream(is_tty=True),
        interval_seconds=0.01,
    )

    with indicator:
        assert indicator.running is False

    assert indicator.running is False


def test_telegram_confirmation_wait_clock_failure_is_silently_disabled() -> None:
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    indicator = TelegramConfirmationWaitIndicator(
        stream=FakeStream(is_tty=False),
        clock=broken_clock,
        interval_seconds=0.01,
    )

    indicator.start()
    indicator.stop()

    assert indicator.running is False


def test_telegram_confirmation_wait_base_exception_from_initial_output_propagates() -> None:
    class InterruptedStream(FakeStream):
        def write(self, value: str) -> int:
            raise KeyboardInterrupt

    indicator = TelegramConfirmationWaitIndicator(
        stream=InterruptedStream(is_tty=True),
        interval_seconds=0.01,
    )

    with pytest.raises(KeyboardInterrupt):
        indicator.start()


def test_telegram_confirmation_wait_thread_start_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StartFailureThread:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("cannot start thread")

        def join(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    stream = FakeStream(is_tty=True)
    monkeypatch.setattr("tg_comment_uploader.terminal_progress.Thread", StartFailureThread)
    indicator = TelegramConfirmationWaitIndicator(stream=stream)

    indicator.start()
    indicator.stop()

    assert indicator.running is False
    assert stream.getvalue().endswith("\n")


def test_telegram_confirmation_wait_join_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JoinFailureThread:
        def __init__(self, **kwargs: object) -> None:
            self.alive = False

        def start(self) -> None:
            self.alive = True

        def join(self) -> None:
            raise RuntimeError("cannot join thread")

        def is_alive(self) -> bool:
            return self.alive

    stream = FakeStream(is_tty=True)
    monkeypatch.setattr("tg_comment_uploader.terminal_progress.Thread", JoinFailureThread)
    indicator = TelegramConfirmationWaitIndicator(stream=stream)

    indicator.start()
    assert indicator.running is True
    indicator.stop()

    assert indicator.running is False
    assert stream.getvalue().endswith("\n")
