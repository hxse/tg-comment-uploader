"""Adapt reupload stages to the shared terminal bar and confirmation indicator."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TextIO

from .reupload_queue import Job
from .terminal_progress import (
    Clock,
    TelegramConfirmationWaitIndicator,
    TerminalProgress,
    format_bytes,
)

REFRESH_SECONDS = 0.2
HEARTBEAT_SECONDS = 1.0
NO_DATA_NOTICE_SECONDS = 15.0


class ReuploadProgress:
    def __init__(self, *, stream: TextIO | None = None, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self.renderer = TerminalProgress(stream=stream, clock=self._clock)
        self.confirmation = TelegramConfirmationWaitIndicator(stream=stream, clock=self._clock)
        self._message_ids: tuple[int, ...] = ()
        self._label: str | None = None
        self._done = self._total = 0
        self._initial_done = 0
        self._started = self._advanced = self._last_render = 0.0
        self._confirming = False

    @asynccontextmanager
    async def tracking(self, job: Job) -> AsyncIterator[None]:
        self._message_ids = tuple(item.message_id for item in job.messages)
        heartbeat = asyncio.create_task(self._heartbeat(), name="reupload-progress")
        try:
            yield
        finally:
            heartbeat.cancel()
            try:
                await asyncio.gather(heartbeat, return_exceptions=True)
            finally:
                self.finish()
                self._message_ids = ()

    def update(self, stage: str, item_index: int, done: int, total: int) -> None:
        message_id = self._message_ids[item_index]
        label = f"input {message_id} ({item_index + 1}/{len(self._message_ids)}) {stage}"
        now = self._clock()
        new_phase = label != self._label or done < self._done
        if new_phase:
            self.renderer.finish()
            self._label = label
            self._started = self._advanced = now
            self._done = 0
            self._initial_done = max(done, 0) if stage == "downloading" else 0
        if done > self._done:
            self._advanced = now
        self._done, self._total = max(done, 0), max(total, 1)
        if new_phase or done >= total or now - self._last_render >= REFRESH_SECONDS:
            self._render(now)

    def upload(self, item_index: int, sent: int, total: int) -> None:
        self.update("uploading", item_index, sent, total)

    def final_request_status(self, active: bool) -> None:
        self._label = None
        if active:
            self.renderer.finish()
            self._confirming = True
            self.confirmation.start()
        else:
            self.confirmation.stop()
            self._confirming = False

    def log(self, message: str) -> None:
        if self._confirming:
            self.confirmation.log(message)
        else:
            self.renderer.log(message)

    def finish(self) -> None:
        self.final_request_status(False)
        self.renderer.finish()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if self._label is not None and self._done < self._total:
                self._render(self._clock())

    def _render(self, now: float) -> None:
        if self._label is None:
            return
        elapsed = max(now - self._started, 0.0)
        idle = max(now - self._advanced, 0.0)
        transferred = max(self._done - self._initial_done, 0)
        rate = transferred / elapsed if elapsed > 0 else 0.0
        if idle >= NO_DATA_NOTICE_SECONDS:
            speed = f"no new data for {int(idle)}s"
        else:
            speed = f"{format_bytes(rate)}/s" if elapsed > 0 and transferred else "--/s"
        self.renderer.update(
            self._label,
            self._done / self._total,
            initial_fraction=self._initial_done / self._total,
            detail=f"{format_bytes(self._done)} / {format_bytes(self._total)}; {speed}",
        )
        self._last_render = now
