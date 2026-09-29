from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions

from reupload_fakes import BOT, FakeBot, forwarded, queue_at, sender_at
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_progress import ReuploadProgress
from tg_comment_uploader.reupload_service import ReuploadService


class CompletionProgress(ReuploadProgress):
    def __init__(self):
        super().__init__(stream=io.StringIO())
        self.summaries: list[str] = []
        self.completed = asyncio.Event()

    def log(self, message: str) -> None:
        super().log(message)
        if message.startswith("batch complete:"):
            self.summaries.append(message)
            self.completed.set()


def test_completion_waits_for_final_confirmation_reports_once_and_resets_next_batch(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))  # Historical work is not part of this batch.
    for message_id in range(27, 32):
        queue.enqueue(forwarded(message_id, kind="document"))
    progress = CompletionProgress()
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedConfirmationBot(FakeBot):
        async def __call__(self, request):
            result = await super().__call__(request)
            if (
                isinstance(request, functions.messages.SendMediaRequest)
                and len(self.delivered) == 5
            ):
                entered.set()
                await release.wait()
            return result

    bot = DelayedConfirmationBot()
    service = ReuploadService(queue, bot_id=BOT, retries=0, settle_seconds=0, progress=progress)
    bot.handler = service.on_message
    with sender_at(tmp_path, bot) as sender:

        async def scenario():
            listener = asyncio.create_task(service.run(sender))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert queue.get("message-31").status == "sending"
                assert not progress.summaries
                release.set()
                await asyncio.wait_for(progress.completed.wait(), 3)
                assert progress.summaries == [
                    "batch complete: 5 task(s), 5 message(s) confirmed; 0 active pending; "
                    "waiting for new forwards (Ctrl+C to stop)"
                ]
                await asyncio.sleep(0.5)
                assert len(progress.summaries) == 1
                progress.completed.clear()
                await service.on_message(SimpleNamespace(message=forwarded(40)))
                await asyncio.wait_for(progress.completed.wait(), 3)
                assert len(progress.summaries) == 2
                assert "1 task(s), 1 message(s) confirmed" in progress.summaries[-1]
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())


def test_incomplete_album_prevents_premature_batch_completion(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.enqueue(forwarded(2, kind="photo", group=7))
    progress = CompletionProgress()
    service = ReuploadService(queue, bot_id=BOT, retries=0, settle_seconds=0, progress=progress)
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:

        async def scenario():
            listener = asyncio.create_task(service.run(sender))
            try:

                async def first_confirmed():
                    while queue.get("message-1").status != "confirmed":
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(first_confirmed(), 3)
                await asyncio.sleep(0.3)
                assert not progress.summaries
                await service.on_message(
                    SimpleNamespace(message=forwarded(3, kind="document", group=7))
                )
                await asyncio.wait_for(progress.completed.wait(), 3)
                assert "2 task(s), 3 message(s) confirmed" in progress.summaries[0]
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())


def test_batch_summary_distinguishes_deferred_work_and_failed_local_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))
    queue.defer_pending()
    queue.enqueue(forwarded(2, kind="document"))
    progress = CompletionProgress()
    service = ReuploadService(queue, bot_id=BOT, retries=0, settle_seconds=0, progress=progress)

    def failed_cleanup(*args):
        raise OSError("cannot delete local files")

    monkeypatch.setattr("tg_comment_uploader.reupload_service.cleanup_job", failed_cleanup)
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:

        async def scenario():
            listener = asyncio.create_task(service.run(sender))
            try:
                await asyncio.wait_for(progress.completed.wait(), 3)
                summary = progress.summaries[0]
                assert "1 task(s), 1 message(s) confirmed; 0 active pending" in summary
                assert "1 deferred task(s) retained" in summary
                assert "1 task(s) awaiting local cleanup" in summary
                assert queue.get("message-1").status == "queued"
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())


def test_failure_after_one_confirmation_does_not_report_the_batch_complete(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, text="ok"))
    queue.enqueue(forwarded(2, text="fail"))
    progress = CompletionProgress()
    service = ReuploadService(queue, bot_id=BOT, retries=0, settle_seconds=0, progress=progress)
    bot = FakeBot()

    def fail_second(request):
        if request.message == "fail":
            bot.fail_final = 1

    bot.send_hook = fail_second
    with sender_at(tmp_path, bot) as sender:
        with pytest.raises(AppError, match="rerun just reupload"):
            sender.run_service(service.run(sender))
    assert queue.get("message-1").status == "confirmed"
    assert queue.get("message-2").status == "sending"
    assert not progress.summaries
