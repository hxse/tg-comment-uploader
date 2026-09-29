"""Bot update intake and one asynchronous, ordered download/send worker."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from telethon import TelegramClient, events, types, utils

from .errors import AppError, RetryableUploadError, TelegramUploadError
from .mtproto_errors import _translate_exception
from .mtproto_sender import MtprotoSender
from .retry_policy import retry_delay_seconds
from .reupload_download import cleanup_job, prepare_job
from .reupload_message import accepts_forward, decode_message
from .reupload_progress import ReuploadProgress
from .reupload_queue import Job, ReuploadQueue

SETTLE_SECONDS = 2.0


class ReuploadService:
    def __init__(
        self,
        queue: ReuploadQueue,
        *,
        bot_id: int,
        retries: int,
        keep_downloads: bool = False,
        settle_seconds: float = SETTLE_SECONDS,
        progress: ReuploadProgress | None = None,
        new_messages_since: int | None = None,
    ) -> None:
        self.queue = queue
        self.bot_id = bot_id
        self.retries = retries
        self.keep_downloads = keep_downloads
        self.settle_seconds = settle_seconds
        self.intake_error: AppError | None = None
        self.started_at = time.time()
        self.new_messages_since = new_messages_since
        self.progress = progress if progress is not None else ReuploadProgress()

    def client_factory(self, *args: Any, **kwargs: Any) -> TelegramClient:
        # Register before start() so even updates arriving during login/peer
        # resolution are durably recorded. Intake handlers never await IO.
        # This must be enabled before connect() restores saved update progress;
        # calling catch_up() after login alone starts from the server's current state.
        client = TelegramClient(*args, **kwargs, sequential_updates=True, catch_up=True)
        client.add_event_handler(self.on_message, events.NewMessage())
        return client

    async def on_message(self, event: Any) -> None:
        try:
            message = event.message
            if accepts_forward(message, self.queue.state.chat_id, self.bot_id):
                # Use the forwarding time in B, not the original post's date.
                # Keep catch-up enabled for reconnects within this command.
                if self.new_messages_since is not None and (
                    message.date is None or message.date.timestamp() < self.new_messages_since
                ):
                    return
                if self.queue.enqueue(message):
                    self.progress.log(f"queued input {message.id}")
        except Exception as exc:
            # Telethon logs/swallow handler failures. Propagate a sanitized
            # failure to the worker instead of losing the input silently.
            self.intake_error = AppError(
                f"could not queue a received message ({type(exc).__name__}); stopped"
            )

    def check_intake(self) -> None:
        if self.intake_error is not None:
            raise self.intake_error
        self.queue.check_problem()

    async def run(self, sender: MtprotoSender) -> None:
        client, peer = await sender.connect()
        if utils.get_peer_id(peer) != self.queue.state.chat_id:
            raise AppError("resolved Telegram peer does not match reupload.chat_id")
        if not isinstance(peer, (types.InputPeerChannel, types.InputPeerUser)):
            raise AppError("reupload supports a channel or a private chat with the bot")
        self.started_at = time.time()
        # Fetch differences using the progress restored by catch_up=True.
        # This is not a history scan and cannot recover an arbitrarily long offline gap.
        await client.catch_up()
        self.progress.log(
            "reupload listening; forward messages to the configured chat (Ctrl+C to stop)",
        )
        # Retry only cleanup requested by the run that confirmed this job. A later
        # default run must not erase files explicitly retained by --keep-downloads.
        for job in self.queue.state.jobs:
            if job.cleanup_pending:
                self._cleanup(job)
        worker = asyncio.create_task(self.work(sender, client, peer))
        disconnected = asyncio.ensure_future(client.disconnected)
        try:
            done, _ = await asyncio.wait(
                (worker, disconnected), return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                await worker
            else:
                # Retrieve any transport exception; only sanitized text leaves
                # this service. The queue retains any uncertain final request.
                try:
                    await disconnected
                except Exception:
                    pass
                raise RetryableUploadError(
                    "Telegram listener disconnected; saved queue will resume"
                )
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

    async def work(self, sender: MtprotoSender, client: Any, peer: Any) -> None:
        completed_tasks = completed_messages = 0
        while True:
            self.check_intake()
            job = self.queue.next_job()
            if job is None:
                if completed_tasks:
                    self._log_batch_complete(completed_tasks, completed_messages)
                    completed_tasks = completed_messages = 0
                await asyncio.sleep(0.2)
                continue
            if time.time() < max(job.received_at, self.started_at) + self.settle_seconds:
                await asyncio.sleep(0.2)
                continue
            # A single member of an album is not enough to know its order.
            # Keep listening for remaining members without sending later jobs.
            if job.status == "queued" and job.grouped_id and len(job.messages) < 2:
                await asyncio.sleep(0.2)
                continue
            await self.process_job(sender, client, peer, job)
            completed_tasks += 1
            completed_messages += len(job.messages)

    def _log_batch_complete(self, tasks: int, messages: int) -> None:
        details = [f"{tasks} task(s), {messages} message(s) confirmed", "0 active pending"]
        deferred = sum(not job.terminal for job in self.queue.state.jobs)
        cleanup = sum(job.cleanup_pending for job in self.queue.state.jobs)
        if deferred:
            details.append(f"{deferred} deferred task(s) retained")
        if cleanup:
            details.append(f"{cleanup} task(s) awaiting local cleanup")
        self.progress.log(
            "batch complete: " + "; ".join(details) + "; waiting for new forwards (Ctrl+C to stop)"
        )

    async def process_job(self, sender: MtprotoSender, client: Any, peer: Any, job: Job) -> None:
        self.check_intake()
        async with self.progress.tracking(job):
            await self._process_job(sender, client, peer, job)

    async def _process_job(self, sender: MtprotoSender, client: Any, peer: Any, job: Job) -> None:
        job = self.queue.begin(job)
        for attempt in range(1, self.retries + 2):
            self.check_intake()
            try:
                uploads = await prepare_job(
                    client,
                    peer,
                    self.queue,
                    self.queue.get(job.key),
                    progress=self.progress.update,
                    status=self.progress.log,
                )
                self.queue.ready(job.key)
                inputs = ",".join(str(m.message_id) for m in job.messages)
                self.progress.log(
                    f"local snapshot ready for inputs {inputs}: all content saved and verified; "
                    "these input messages are no longer needed for this task"
                )

                def before_send() -> None:
                    self.check_intake()
                    self.queue.sending(job.key)

                self.progress.log(f"uploading {job.key} ({len(job.messages)} message(s))")
                if uploads:
                    output_ids = await sender.send_prepared(
                        uploads,
                        random_ids=job.random_ids,
                        before_final_request=before_send,
                        progress_callback=self.progress.upload,
                    )
                else:
                    message = decode_message(job.messages[0].snapshot)
                    output_ids = await sender.send_text(
                        message.message or "",
                        entities=tuple(message.entities or ()),
                        random_id=job.random_ids[0],
                        before_final_request=before_send,
                    )
                self.queue.confirm(job.key, output_ids, cleanup_pending=not self.keep_downloads)
                self.progress.log(f"confirmed {job.key}: {','.join(map(str, output_ids))}")
                if not self.keep_downloads:
                    self._cleanup(self.queue.get(job.key))
                return
            except asyncio.CancelledError:
                # A transport-cancelled request and user interruption both keep
                # the same saved random IDs. The outer listener handles reconnect.
                raise
            except AppError as exc:
                if not isinstance(exc, TelegramUploadError):
                    raise
                error = exc
            except Exception as exc:
                error = _translate_exception(
                    exc,
                    final_request_started=self.queue.get(job.key).status == "sending",
                    secrets=sender._secrets,
                    context=f"backing up {job.key}",
                )
            if not isinstance(error, RetryableUploadError):
                raise error
            if client.is_connected() is False:
                raise error
            if attempt > self.retries:
                raise AppError(
                    f"{job.key} failed after {attempt} attempts; rerun just reupload to resume "
                    f"the saved queue: {error}"
                ) from error
            delay = retry_delay_seconds(error, failed_attempt=attempt)
            self.progress.finish()
            self.progress.log(f"retrying {job.key} in {delay}s ({attempt}/{self.retries}): {error}")
            await asyncio.sleep(delay)

    def _cleanup(self, job: Job) -> None:
        try:
            cleanup_job(self.queue, job)
        except (OSError, AppError) as exc:
            self.progress.log(
                f"could not remove downloads for confirmed {job.key} ({type(exc).__name__})",
            )
