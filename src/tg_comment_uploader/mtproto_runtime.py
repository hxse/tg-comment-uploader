"""Owned event-loop execution and cancellation for both commands."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from .errors import NonRetryableUploadError, RetryableUploadError

if TYPE_CHECKING:
    from .mtproto_sender import MtprotoSender

OperationResultT = TypeVar("OperationResultT")


def _run_operation(
    self: MtprotoSender,
    operation: Coroutine[Any, Any, OperationResultT],
) -> OperationResultT:
    """Run one tracked send operation on the sender's owned event loop."""

    if self._operation_abort_requested:
        operation.close()
        raise NonRetryableUploadError(
            "Telegram sender cannot be reused after an interrupted operation"
        )
    if self._active_operation is not None:
        operation.close()
        raise NonRetryableUploadError("another Telegram send operation is already active")

    loop = self._runner.get_loop()
    task: asyncio.Task[OperationResultT] | None = None
    try:
        self._operation_in_progress = True
        self._final_request_started_for_operation = False
        task = loop.create_task(operation)
        self._active_operation = task
        # Python 3.12 Runner.run() only accepts coroutine objects, not
        # Task instances, so a tiny coroutine awaits the explicitly
        # tracked child task while preserving Runner's SIGINT handling.
        return self._runner.run(_await_operation_task(task))
    except Exception:
        raise
    except asyncio.CancelledError as exc:
        # A Telethon request future is cancelled when its transport is
        # torn down. That cancellation reaches this task without anyone
        # calling task.cancel(), so cancelling() remains zero. Requiring
        # both signals avoids turning an explicit user/task cancellation
        # into an automatic retry merely because the socket also closed.
        transport_cancelled = (
            task is not None
            and task.cancelling() == 0
            and _transport_is_definitively_disconnected(self)
        )
        if transport_cancelled:
            final_request_started = self._final_request_started_for_operation
            if final_request_started:
                message = (
                    "Telegram disconnected while waiting for the final send result; "
                    "delivery could not be confirmed"
                )
            else:
                message = (
                    "Telegram disconnected while uploading data before the final send; "
                    "retrying from the beginning is safe"
                )
            raise RetryableUploadError(
                message,
                outcome_uncertain=final_request_started,
                final_request_started=final_request_started,
            ) from exc
        _cancel_and_drain_after_interrupt(self)
        raise
    except BaseException:
        # SIGTERM is translated to KeyboardInterrupt by the CLI. It can
        # interrupt Runner.run() while its tasks are still pending. Cancel
        # them before any later loop re-entry (especially disconnect), then
        # drain them so none can cross the final-send boundary afterward.
        _cancel_and_drain_after_interrupt(self)
        raise
    finally:
        if task is None:
            operation.close()
            self._operation_in_progress = False
        elif self._active_operation is task and task.done() and not self._operation_abort_requested:
            self._active_operation = None
            self._operation_in_progress = False
        self._final_request_started_for_operation = False


def _cancel_and_drain_after_interrupt(self: MtprotoSender) -> None:
    try:
        _cancel_and_drain_active_operation(self)
    except BaseException:
        # Preserve the original interrupt. close() will retry cleanup, and
        # no loop re-entry can occur before the active task has at least
        # been synchronously marked for cancellation.
        pass


def _transport_is_definitively_disconnected(self: MtprotoSender) -> bool:
    if self._closed or self._operation_abort_requested:
        return False
    client = self._client
    if client is None:
        return False
    is_connected = getattr(client, "is_connected", None)
    if not callable(is_connected):
        return False
    try:
        return is_connected() is False
    except Exception:
        return False


def _cancel_and_drain_active_operation(self: MtprotoSender) -> None:
    task = self._active_operation
    if task is None and not self._operation_in_progress:
        return

    # Mark every task created on this sender-owned loop before allowing the
    # loop to run again. This includes Runner.run()'s small wrapper task and
    # any Telethon child tasks, so even an interrupt that arrived before the
    # wrapper's first step cannot leave executable upload work behind.
    self._operation_abort_requested = True
    if task is not None and not task.done():
        task.cancel()
    loop = self._runner.get_loop()
    operation_tasks = set(asyncio.all_tasks(loop))
    if task is not None:
        # A signal raised while the child was executing can leave it done
        # with KeyboardInterrupt before Runner's wrapper retrieved the
        # exception. Drain it too, avoiding an unobserved task exception.
        operation_tasks.add(task)
    frozen_operation_tasks = tuple(operation_tasks)
    for operation_task in frozen_operation_tasks:
        if not operation_task.done():
            operation_task.cancel()
    try:
        if frozen_operation_tasks:
            self._runner.run(_drain_cancelled_tasks(frozen_operation_tasks))
    finally:
        if all(operation_task.done() for operation_task in frozen_operation_tasks):
            self._active_operation = None
            self._operation_in_progress = False


async def _disconnect_client(client: Any) -> None:
    await client.disconnect()


def _observe_disconnected_future(client: Any) -> None:
    try:
        disconnected = getattr(client, "disconnected", None)
    except Exception:
        # Observing a diagnostic lifecycle future must never alter Telegram
        # delivery or cleanup semantics.
        return

    if not isinstance(disconnected, asyncio.Future):
        return
    if disconnected.done():
        _consume_future_exception(disconnected)
    else:
        disconnected.add_done_callback(_consume_future_exception)


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    try:
        # exception() marks the outcome as retrieved without removing it;
        # another waiter can still observe the same result or exception.
        future.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        pass


async def _await_operation_task(
    task: asyncio.Task[OperationResultT],
) -> OperationResultT:
    return await task


async def _drain_cancelled_tasks(tasks: tuple[asyncio.Task[Any], ...]) -> None:
    for task in tasks:
        try:
            await task
        except BaseException:
            # The original interruption is propagated by the synchronous
            # caller. Cleanup must consume task outcomes without replacing it.
            pass
