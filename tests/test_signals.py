from __future__ import annotations

import signal
from typing import Any

import pytest

from tg_comment_uploader.cli import termination_as_interrupt


def test_sigterm_is_translated_to_keyboard_interrupt_and_handler_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = object()
    installed: list[Any] = []

    monkeypatch.setattr(signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.append(handler))

    with pytest.raises(KeyboardInterrupt):
        with termination_as_interrupt():
            installed[0](signal.SIGTERM, None)

    assert installed[-1] is previous
