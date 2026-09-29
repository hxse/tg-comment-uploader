from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, FakeBot, forwarded, queue_at, sender_at
from test_terminal_progress import FakeClock, FakeStream
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_progress import ReuploadProgress
from tg_comment_uploader.reupload_service import ReuploadService
from tg_comment_uploader.terminal_progress import (
    TELEGRAM_CONFIRMATION_WAIT_MESSAGE,
    TelegramConfirmationWaitIndicator,
    TerminalProgress,
)


def test_changing_byte_details_does_not_restart_the_shared_timer() -> None:
    stream = FakeStream(is_tty=True)
    clock = FakeClock()
    renderer = TerminalProgress(stream=stream, clock=clock)
    renderer.update("download", 0, detail="0 / 1 MiB")
    clock.now = 10
    renderer.update("download", 0.5, detail="0.5 / 1 MiB")
    assert "elapsed 0:10 eta 0:10 0.5 / 1 MiB" in stream.getvalue()


def test_confirmation_logs_do_not_interleave_with_spinner_output() -> None:
    stream = FakeStream(is_tty=True)
    wait = TelegramConfirmationWaitIndicator(stream=stream, interval_seconds=60)
    with wait:
        wait.log("queued input 99")
        assert stream.getvalue().endswith("(elapsed 0:00)\nqueued input 99\n")
    assert not wait.running


@pytest.mark.parametrize("is_tty", [False, True])
def test_progress_reports_bytes_speed_and_album_member_order(tmp_path: Path, is_tty: bool) -> None:
    queue = queue_at(tmp_path)
    for message_id in (5, 6):
        queue.enqueue(forwarded(message_id, kind="document", group=1))
    job = queue.next_job()
    assert job is not None
    stream, clock = FakeStream(is_tty=is_tty), FakeClock()
    progress = ReuploadProgress(stream=stream, clock=clock)

    async def scenario() -> None:
        async with progress.tracking(job):
            progress.update("downloading", 0, 0, 1024)
            clock.now = 10
            progress.update("downloading", 0, 512, 1024)
            progress.log("queued input 99")
            clock.now = 20
            progress.update("downloading", 0, 1024, 1024)
            progress.update("downloading", 1, 0, 1024)
            clock.now = 30
            progress.update("downloading", 1, 1024, 1024)
        assert not any(t.get_name() == "reupload-progress" for t in asyncio.all_tasks())

    asyncio.run(scenario())
    output = stream.getvalue()
    assert "input 5 (1/2) downloading" in output
    assert "input 6 (2/2) downloading" in output
    assert "512 B / 1.0 KiB; 51 B/s" in output
    assert "50.0% elapsed 0:10 eta 0:10" in output
    assert "queued input 99\n" in output
    assert output.endswith("\n")
    assert ("\r" in output) is is_tty


@pytest.mark.parametrize("is_tty", [False, True])
def test_idle_download_keeps_reporting_without_progress_callbacks(
    tmp_path: Path,
    monkeypatch,
    is_tty: bool,
) -> None:
    monkeypatch.setattr("tg_comment_uploader.reupload_progress.HEARTBEAT_SECONDS", 0.01)
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(5, kind="document"))
    job = queue.next_job()
    assert job is not None
    stream, clock = FakeStream(is_tty=is_tty), FakeClock()
    progress = ReuploadProgress(stream=stream, clock=clock)

    async def scenario() -> None:
        async with progress.tracking(job):
            progress.update("downloading", 0, 0, 1024)
            clock.now = 20
            await asyncio.sleep(0.04)
            output = stream.getvalue()
            assert "elapsed 0:20" in output
            assert "no new data for 20s" in output
            if not is_tty:
                assert len(output.splitlines()) == 2  # No log flood from heartbeat ticks.
            clock.now = 21
            progress.update("downloading", 0, 512, 1024)
            clock.now = 40
            progress.update("downloading", 0, 1024, 1024)
            assert "no new data" not in stream.getvalue().splitlines()[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize("album", [False, True])
def test_real_callbacks_cover_download_hash_upload_confirmation_and_incoming_logs(
    tmp_path: Path,
    album: bool,
) -> None:
    messages = [forwarded(5, kind="document", group=1 if album else None)]
    if album:
        messages.append(forwarded(6, kind="photo", group=1))
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    stream = FakeStream(is_tty=True)
    progress = ReuploadProgress(stream=stream)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True, progress=progress)
    bot = FakeBot(messages)

    async def during_download(message) -> None:
        assert f"input {message.id}" in stream.getvalue()
        assert "downloading" in stream.getvalue() and "0.0%" in stream.getvalue()
        await service.on_message(SimpleNamespace(message=forwarded(9)))

    async def during_confirmation(event) -> None:
        assert progress.confirmation.running
        assert TELEGRAM_CONFIRMATION_WAIT_MESSAGE in stream.getvalue()
        await service.on_message(SimpleNamespace(message=forwarded(10)))

    bot.download_hook = during_download
    bot.handler = during_confirmation
    with sender_at(tmp_path, bot, final_request_status=progress.final_request_status) as sender:

        async def scenario() -> None:
            client, peer = await sender.connect()
            job = queue.next_job()
            assert job is not None
            await service.process_job(sender, client, peer, job)

        sender.run_service(scenario())
    output = stream.getvalue()
    for i, message in enumerate(messages, start=1):
        prefix = f"input {message.id} ({i}/{len(messages)})"
        for stage in ("downloading", "verifying download", "uploading"):
            assert f"{prefix} {stage}" in output
    assert output.index("verifying download") < output.index(TELEGRAM_CONFIRMATION_WAIT_MESSAGE)
    assert "queued input 9\n" in output and "queued input 10\n" in output
    assert "confirmed " in output and queue.state.jobs[0].status == "confirmed"
    assert not progress.confirmation.running
    assert bot.sent_payloads == [bot.payload] * len(messages)


def test_retry_reuses_download_and_finishes_previous_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_service.retry_delay_seconds", lambda *a, **k: 0
    )
    message = forwarded(5, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    stream = FakeStream(is_tty=True)
    progress = ReuploadProgress(stream=stream)
    service = ReuploadService(queue, bot_id=BOT, retries=1, keep_downloads=True, progress=progress)
    bot = FakeBot([message])
    bot.fail_final = 1
    with sender_at(tmp_path, bot, final_request_status=progress.final_request_status) as sender:

        async def scenario() -> None:
            client, peer = await sender.connect()
            job = queue.next_job()
            assert job is not None
            await service.process_job(sender, client, peer, job)

        sender.run_service(scenario())
    output = stream.getvalue()
    assert "retrying message-5" in output
    assert "verifying cached download" in output
    assert output.count("uploading [") == 4  # Start and end for each of two attempts.
    assert [n for event, n in bot.timeline if event == "download"] == [5]
    assert len(bot.delivered) == 1 and not progress.confirmation.running


@pytest.mark.parametrize("interrupt", [False, True])
def test_failure_and_cancellation_close_progress_and_confirmation(
    tmp_path: Path, interrupt: bool
) -> None:
    message = forwarded(5, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    stream = FakeStream(is_tty=True)
    progress = ReuploadProgress(stream=stream)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True, progress=progress)
    bot = FakeBot([message])

    def fail(request) -> None:
        assert progress.confirmation.running
        if interrupt:
            raise asyncio.CancelledError
        raise TimeoutError("test send timed out")

    bot.send_hook = fail
    with sender_at(tmp_path, bot, final_request_status=progress.final_request_status) as sender:

        async def scenario() -> None:
            client, peer = await sender.connect()
            job = queue.next_job()
            assert job is not None
            with pytest.raises(asyncio.CancelledError if interrupt else AppError):
                await service.process_job(sender, client, peer, job)
            assert not any(t.get_name() == "reupload-progress" for t in asyncio.all_tasks())

        sender.run_service(scenario())
    assert not progress.confirmation.running
    assert stream.getvalue().endswith("\n")
    assert queue.state.jobs[0].status == "sending"
