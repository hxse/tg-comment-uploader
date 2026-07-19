"""Telethon-backed MTProto implementation of the synchronous sender facade."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import re
import secrets
import stat
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, TypeVar

from telethon import TelegramClient, errors, functions, types, utils

from .errors import (
    NonRetryableUploadError,
    RetryableUploadError,
    TelegramUploadError,
    sanitize_untrusted_error_text,
)
from .telegram_sender import (
    BeforeFinalRequest,
    FinalRequestStatus,
    MediaGroupUploadProgress,
    PeerResolved,
    TELEGRAM_INT32_MAX,
    UploadItem,
    UploadProgress,
    parse_bot_id,
    validate_upload_file_size,
)
from .upload_contract import MEDIA_GROUP_MAX_ITEMS, MEDIA_GROUP_MIN_ITEMS

MIN_RANDOM_ID = -(2**63)
MAX_RANDOM_ID = 2**63 - 1
FINAL_REQUEST_TIMEOUT_SECONDS = 10 * 60
UPLOAD_PART_SIZE_BYTES = 512 * 1024
UPLOAD_MAX_IN_FLIGHT_PARTS = 8
TELEGRAM_BIG_FILE_THRESHOLD_BYTES = 10 * 1024 * 1024
NUMERIC_CHAT_ID_PATTERN = re.compile(r"-?[0-9]+")
SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")

ClientFactory = Callable[..., Any]
RunnerFactory = Callable[[], asyncio.Runner]
OperationResultT = TypeVar("OperationResultT")


class _ResponseValidationError(Exception):
    """A final request returned Updates that cannot safely confirm delivery."""


class MtprotoSender:
    """Own one Telethon client and event loop for a complete upload command."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_path: Path,
        chat_id: str | int,
        reply_message_id: int | None,
        supports_streaming: bool,
        final_request_status: FinalRequestStatus | None = None,
        peer_resolved: PeerResolved | None = None,
        final_request_timeout_seconds: float = FINAL_REQUEST_TIMEOUT_SECONDS,
        client_factory: ClientFactory = TelegramClient,
        runner_factory: RunnerFactory = asyncio.Runner,
    ) -> None:
        if (
            isinstance(api_id, bool)
            or not isinstance(api_id, int)
            or not 1 <= api_id <= TELEGRAM_INT32_MAX
        ):
            raise NonRetryableUploadError(
                "Telegram api_id must be a positive signed 32-bit integer"
            )
        if not isinstance(api_hash, str) or not api_hash:
            raise NonRetryableUploadError("Telegram api_hash must be a non-empty string")
        if not isinstance(bot_token, str) or not bot_token:
            raise NonRetryableUploadError("Telegram bot token must be a non-empty string")
        if reply_message_id is not None and (
            isinstance(reply_message_id, bool)
            or not isinstance(reply_message_id, int)
            or not 1 <= reply_message_id <= TELEGRAM_INT32_MAX
        ):
            raise NonRetryableUploadError(
                "reply_message_id must be a positive signed 32-bit integer"
            )
        if not isinstance(supports_streaming, bool):
            raise NonRetryableUploadError("supports_streaming must be a boolean")
        if (
            isinstance(final_request_timeout_seconds, bool)
            or not isinstance(final_request_timeout_seconds, (int, float))
            or not math.isfinite(final_request_timeout_seconds)
            or final_request_timeout_seconds <= 0
        ):
            raise NonRetryableUploadError(
                "final Telegram request timeout must be finite and positive"
            )

        self._api_id = api_id
        self._api_hash = api_hash
        self._bot_token = bot_token
        try:
            self._expected_bot_id = parse_bot_id(bot_token)
        except ValueError as exc:
            raise NonRetryableUploadError(f"Telegram bot token {exc}") from exc
        self._session_path = Path(session_path)
        self._chat_id = normalize_chat_id(chat_id)
        self._reply_message_id = reply_message_id
        self._supports_streaming = supports_streaming
        self._final_request_status = final_request_status
        self._peer_resolved = peer_resolved
        self._final_request_timeout_seconds = float(final_request_timeout_seconds)
        self._client_factory = client_factory
        self._runner = runner_factory()
        self._client: Any | None = None
        self._peer: Any | None = None
        self._active_operation: asyncio.Task[Any] | None = None
        self._operation_in_progress = False
        self._operation_abort_requested = False
        self._final_request_started_for_operation = False
        self._closed = False

        _prepare_private_session_directory(self._session_path)
        _protect_session_path_candidates(self._session_path)

    def __enter__(self) -> MtprotoSender:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        try:
            self.close()
        except TelegramUploadError:
            if exc_type is None and exc_value is None:
                raise

    def close(self) -> None:
        """Cancel and drain any send, then disconnect and close the owned event loop."""

        if self._closed:
            return
        self._closed = True
        disconnect_error: BaseException | None = None
        try:
            try:
                self._cancel_and_drain_active_operation()
            except BaseException as exc:
                disconnect_error = exc
            operation_drained = not self._operation_in_progress

            client = self._client
            self._client = None
            self._peer = None
            if client is not None and operation_drained:
                try:
                    self._runner.run(_disconnect_client(client))
                except BaseException as exc:  # runner.close must still happen
                    if disconnect_error is None:
                        disconnect_error = exc
            if client is not None:
                try:
                    _protect_session_files(client)
                except BaseException as exc:
                    if disconnect_error is None:
                        disconnect_error = exc
        finally:
            try:
                self._runner.close()
            except BaseException as exc:
                if disconnect_error is None:
                    disconnect_error = exc

        if disconnect_error is not None:
            if isinstance(disconnect_error, (KeyboardInterrupt, SystemExit)):
                raise disconnect_error
            raise _translate_exception(
                disconnect_error,
                final_request_started=False,
                secrets=self._secrets,
                context="disconnecting from Telegram",
            ) from disconnect_error

    def send_video(
        self,
        item: UploadItem,
        *,
        random_id: int,
        progress_callback: UploadProgress | None = None,
        before_final_request: BeforeFinalRequest | None = None,
    ) -> int:
        """Upload and send one video with the caller's persistent random ID."""

        self._assert_open()
        validated_random_id = _validate_random_id(random_id)
        _validate_upload_item(item)
        try:
            return int(
                self._run_operation(
                    self._send_video(
                        item,
                        random_id=validated_random_id,
                        progress_callback=progress_callback,
                        before_final_request=before_final_request,
                    )
                )
            )
        except TelegramUploadError:
            self._drop_connection_after_failure()
            raise
        except Exception as exc:
            raise _translate_exception(
                exc,
                final_request_started=False,
                secrets=self._secrets,
                context=f"uploading {item.path}",
            ) from exc

    def send_media_group(
        self,
        items: Sequence[UploadItem],
        *,
        random_ids: Sequence[int],
        progress_callback: MediaGroupUploadProgress | None = None,
        before_final_request: BeforeFinalRequest | None = None,
    ) -> tuple[int, ...]:
        """Upload and atomically send one album using caller-persisted random IDs."""

        self._assert_open()
        frozen_items = tuple(items)
        if not MEDIA_GROUP_MIN_ITEMS <= len(frozen_items) <= MEDIA_GROUP_MAX_ITEMS:
            raise NonRetryableUploadError(
                f"a Telegram media group must contain {MEDIA_GROUP_MIN_ITEMS} to "
                f"{MEDIA_GROUP_MAX_ITEMS} items"
            )
        for item in frozen_items:
            _validate_upload_item(item)

        validated_random_ids = _validate_random_ids(random_ids)
        if len(validated_random_ids) != len(frozen_items):
            raise NonRetryableUploadError(
                "media-group random ID count must match the upload item count"
            )

        try:
            result = self._run_operation(
                self._send_media_group(
                    frozen_items,
                    random_ids=validated_random_ids,
                    progress_callback=progress_callback,
                    before_final_request=before_final_request,
                )
            )
            return tuple(int(message_id) for message_id in result)
        except TelegramUploadError:
            self._drop_connection_after_failure()
            raise
        except Exception as exc:
            raise _translate_exception(
                exc,
                final_request_started=False,
                secrets=self._secrets,
                context="uploading a Telegram media group",
            ) from exc

    def _run_operation(
        self,
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
                and self._transport_is_definitively_disconnected()
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
            self._cancel_and_drain_after_interrupt()
            raise
        except BaseException:
            # SIGTERM is translated to KeyboardInterrupt by the CLI. It can
            # interrupt Runner.run() while its tasks are still pending. Cancel
            # them before any later loop re-entry (especially disconnect), then
            # drain them so none can cross the final-send boundary afterward.
            self._cancel_and_drain_after_interrupt()
            raise
        finally:
            if task is None:
                operation.close()
                self._operation_in_progress = False
            elif (
                self._active_operation is task
                and task.done()
                and not self._operation_abort_requested
            ):
                self._active_operation = None
                self._operation_in_progress = False
            self._final_request_started_for_operation = False

    def _cancel_and_drain_after_interrupt(self) -> None:
        try:
            self._cancel_and_drain_active_operation()
        except BaseException:
            # Preserve the original interrupt. close() will retry cleanup, and
            # no loop re-entry can occur before the active task has at least
            # been synchronously marked for cancellation.
            pass

    def _transport_is_definitively_disconnected(self) -> bool:
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

    def _cancel_and_drain_active_operation(self) -> None:
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

    def _raise_if_operation_aborted(self) -> None:
        if self._closed or self._operation_abort_requested:
            raise asyncio.CancelledError

    @property
    def _secrets(self) -> tuple[str, str]:
        return self._bot_token, self._api_hash

    def _assert_open(self) -> None:
        if self._closed:
            raise NonRetryableUploadError("Telegram sender is already closed")

    async def _ensure_connected(self) -> tuple[Any, Any]:
        if self._client is not None and self._peer is not None:
            return self._client, self._peer

        try:
            client = self._client_factory(
                str(self._session_path),
                self._api_id,
                self._api_hash,
                request_retries=0,
                connection_retries=0,
                retry_delay=0,
                auto_reconnect=False,
                flood_sleep_threshold=0,
                raise_last_call_error=True,
            )
            self._client = client
            _protect_session_files(client)
            await client.start(bot_token=self._bot_token)
            me = await client.get_me()
            if me is None or getattr(me, "bot", None) is not True:
                raise NonRetryableUploadError(
                    "the persistent Telegram session is not authenticated as a bot"
                )
            actual_bot_id = getattr(me, "id", None)
            if (
                isinstance(actual_bot_id, bool)
                or not isinstance(actual_bot_id, int)
                or actual_bot_id != self._expected_bot_id
            ):
                raise NonRetryableUploadError(
                    "the persistent Telegram session belongs to a different bot token"
                )
            _protect_session_files(client)
            peer = await self._resolve_peer(client)
            _protect_session_files(client)
            peer_id = utils.get_peer_id(peer)
            if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
                raise NonRetryableUploadError("Telegram resolved chat_id to an invalid peer")
            if self._peer_resolved is not None:
                self._peer_resolved(peer_id)
            self._peer = peer
            return client, peer
        except BaseException as exc:
            await self._discard_failed_client()
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            if isinstance(exc, TelegramUploadError):
                raise
            raise _translate_exception(
                exc,
                final_request_started=False,
                secrets=self._secrets,
                context="connecting to Telegram",
            ) from exc

    async def _resolve_peer(self, client: Any) -> Any:
        try:
            if isinstance(self._chat_id, str):
                # Unlike get_input_entity(str), get_entity(str) performs a fresh
                # public API lookup, so a renamed username cannot silently reuse a
                # stale peer from the persistent session cache.
                return utils.get_input_peer(await client.get_entity(self._chat_id))
            return await client.get_input_entity(self._chat_id)
        except (TypeError, ValueError) as exc:
            raise NonRetryableUploadError(
                f"Telegram could not resolve chat_id {self._chat_id!r}; make sure the bot "
                "can access that channel or group"
            ) from exc

    async def _send_video(
        self,
        item: UploadItem,
        *,
        random_id: int,
        progress_callback: UploadProgress | None,
        before_final_request: BeforeFinalRequest | None,
    ) -> int:
        client, peer = await self._ensure_connected()
        self._raise_if_operation_aborted()
        media = await self._upload_document(
            client,
            item,
            progress_callback=progress_callback,
        )
        self._raise_if_operation_aborted()
        request = functions.messages.SendMediaRequest(
            peer=peer,
            media=media,
            message=item.caption,
            entities=None,
            reply_to=self._reply_to,
            random_id=random_id,
        )
        result = await self._invoke_final_request(
            client,
            request,
            before_final_request=before_final_request,
        )
        try:
            return parse_send_result(
                result,
                (random_id,),
                expected_peer_id=utils.get_peer_id(peer),
                expect_grouped=False,
            )[0]
        except _ResponseValidationError as exc:
            raise RetryableUploadError(
                f"Telegram returned an incomplete send result: {exc}",
                outcome_uncertain=True,
                final_request_started=True,
            ) from exc

    async def _send_media_group(
        self,
        items: tuple[UploadItem, ...],
        *,
        random_ids: tuple[int, ...],
        progress_callback: MediaGroupUploadProgress | None,
        before_final_request: BeforeFinalRequest | None,
    ) -> tuple[int, ...]:
        client, peer = await self._ensure_connected()
        self._raise_if_operation_aborted()
        multi_media: list[types.InputSingleMedia] = []

        for item_index, (item, random_id) in enumerate(zip(items, random_ids, strict=True)):
            self._raise_if_operation_aborted()
            per_file_progress: UploadProgress | None = None
            if progress_callback is not None:

                def report_file_progress(
                    sent: int,
                    total: int,
                    *,
                    index: int = item_index,
                ) -> None:
                    progress_callback(index, sent, total)

                per_file_progress = report_file_progress
            uploaded_media = await self._upload_document(
                client,
                item,
                progress_callback=per_file_progress,
            )
            self._raise_if_operation_aborted()
            try:
                materialized = await client(
                    functions.messages.UploadMediaRequest(
                        peer=peer,
                        media=uploaded_media,
                    )
                )
                self._raise_if_operation_aborted()
                document = getattr(materialized, "document", None)
                if not isinstance(document, types.Document):
                    raise NonRetryableUploadError(
                        "Telegram did not materialize an uploaded video as a document"
                    )
                reusable_media = types.InputMediaDocument(id=utils.get_input_document(document))
            except TelegramUploadError:
                raise
            except Exception as exc:
                raise _translate_exception(
                    exc,
                    final_request_started=False,
                    secrets=self._secrets,
                    context=f"preparing media-group item {item_index + 1}",
                ) from exc

            multi_media.append(
                types.InputSingleMedia(
                    media=reusable_media,
                    message=item.caption,
                    entities=None,
                    random_id=random_id,
                )
            )

        request = functions.messages.SendMultiMediaRequest(
            peer=peer,
            multi_media=multi_media,
            reply_to=self._reply_to,
        )
        result = await self._invoke_final_request(
            client,
            request,
            before_final_request=before_final_request,
        )
        try:
            return parse_send_result(
                result,
                random_ids,
                expected_peer_id=utils.get_peer_id(peer),
                expect_grouped=True,
            )
        except _ResponseValidationError as exc:
            raise RetryableUploadError(
                f"Telegram returned an incomplete media-group result: {exc}",
                outcome_uncertain=True,
                final_request_started=True,
            ) from exc

    async def _upload_document(
        self,
        client: Any,
        item: UploadItem,
        *,
        progress_callback: UploadProgress | None,
    ) -> types.InputMediaUploadedDocument:
        try:
            file_handle = item.path.open("rb")
        except OSError as exc:
            raise NonRetryableUploadError(f"failed to open upload file {item.path}: {exc}") from exc

        try:
            with file_handle:
                try:
                    initial_stat = os.fstat(file_handle.fileno())
                    file_size = _regular_file_size(file_handle, item.path)
                    if file_size != item.expected_size:
                        raise NonRetryableUploadError(
                            f"upload file size no longer matches its preflight identity: "
                            f"{item.path}; expected {item.expected_size:,} bytes, "
                            f"found {file_size:,} bytes"
                        )
                    attributes, mime_type = utils.get_attributes(
                        file_handle,
                        supports_streaming=self._supports_streaming,
                    )
                    if not any(
                        isinstance(attribute, types.DocumentAttributeVideo)
                        for attribute in attributes
                    ):
                        attributes.append(
                            types.DocumentAttributeVideo(
                                duration=0,
                                w=1,
                                h=1,
                                supports_streaming=self._supports_streaming,
                            )
                        )
                    file_handle.seek(0)
                except TelegramUploadError:
                    raise
                except OSError as exc:
                    raise NonRetryableUploadError(
                        f"failed to inspect upload file {item.path}: {exc}"
                    ) from exc

                callback: UploadProgress | None = None
                if progress_callback is not None:

                    def report_progress(sent: int, total: int) -> None:
                        progress_callback(int(sent), int(total))

                    callback = report_progress
                hashing_reader = _SequentialHashingReader(file_handle)
                try:
                    uploaded = await self._upload_file_pipelined(
                        client,
                        hashing_reader,
                        file_size=file_size,
                        file_name=item.path.name,
                        progress_callback=callback,
                    )
                    self._raise_if_operation_aborted()
                except TelegramUploadError:
                    raise
                except Exception as exc:
                    raise _translate_exception(
                        exc,
                        final_request_started=False,
                        secrets=self._secrets,
                        context=f"uploading file data for {item.path}",
                    ) from exc

                try:
                    position = file_handle.tell()
                    bytes_read = hashing_reader.bytes_read
                    actual_sha256 = hashing_reader.hexdigest()
                    grew_after_expected_eof = file_handle.read(1) != b""
                    final_stat = os.fstat(file_handle.fileno())
                except OSError as exc:
                    raise NonRetryableUploadError(
                        f"failed to verify upload file {item.path}: {exc}"
                    ) from exc
                if (
                    position != file_size
                    or bytes_read != file_size
                    or grew_after_expected_eof
                    or final_stat.st_size != initial_stat.st_size
                    or final_stat.st_dev != initial_stat.st_dev
                    or final_stat.st_ino != initial_stat.st_ino
                    or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
                ):
                    raise NonRetryableUploadError(
                        f"upload file changed while reading it: {item.path}"
                    )
                if not hmac.compare_digest(
                    actual_sha256,
                    item.expected_sha256,
                ):
                    raise NonRetryableUploadError(
                        f"upload file content no longer matches its preflight identity: {item.path}"
                    )
        except TelegramUploadError:
            raise

        return types.InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type,
            attributes=attributes,
            nosound_video=True,
        )

    async def _upload_file_pipelined(
        self,
        client: Any,
        stream: _SequentialHashingReader,
        *,
        file_size: int,
        file_name: str,
        progress_callback: UploadProgress | None,
    ) -> types.InputFile | types.InputFileBig:
        """Upload one file with a bounded window of MTProto part requests."""

        part_count = (file_size + UPLOAD_PART_SIZE_BYTES - 1) // UPLOAD_PART_SIZE_BYTES
        file_id = secrets.randbits(63) or 1
        is_big = file_size > TELEGRAM_BIG_FILE_THRESHOLD_BYTES
        md5_digest = hashlib.md5(usedforsecurity=False)
        acknowledged_bytes = 0
        pending: set[asyncio.Task[int]] = set()

        async def send_part(part_index: int, part: bytes) -> int:
            request: Any
            if is_big:
                request = functions.upload.SaveBigFilePartRequest(
                    file_id=file_id,
                    file_part=part_index,
                    file_total_parts=part_count,
                    bytes=part,
                )
            else:
                request = functions.upload.SaveFilePartRequest(
                    file_id=file_id,
                    file_part=part_index,
                    bytes=part,
                )
            result = await client(request)
            if result is not True:
                raise RuntimeError(f"Telegram rejected upload file part {part_index}")
            return len(part)

        async def acknowledge_completed_part() -> None:
            nonlocal acknowledged_bytes, pending
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed_sizes: list[int] = []
            first_error: BaseException | None = None
            for task in done:
                try:
                    completed_sizes.append(task.result())
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
            for completed_size in completed_sizes:
                acknowledged_bytes += completed_size
                if progress_callback is not None:
                    progress_callback(acknowledged_bytes, file_size)

        try:
            for part_index in range(part_count):
                self._raise_if_operation_aborted()
                expected_size = min(
                    UPLOAD_PART_SIZE_BYTES,
                    file_size - part_index * UPLOAD_PART_SIZE_BYTES,
                )
                part = stream.read(expected_size)
                if len(part) != expected_size:
                    raise NonRetryableUploadError(
                        f"upload file ended early while reading part {part_index}; "
                        f"expected {expected_size:,} bytes, found {len(part):,}"
                    )
                if not is_big:
                    md5_digest.update(part)
                pending.add(asyncio.create_task(send_part(part_index, part)))
                if len(pending) >= UPLOAD_MAX_IN_FLIGHT_PARTS:
                    await acknowledge_completed_part()

            while pending:
                await acknowledge_completed_part()
            self._raise_if_operation_aborted()
        except BaseException:
            for task in pending:
                task.cancel()
            await _drain_cancelled_tasks(tuple(pending))
            raise

        if is_big:
            return types.InputFileBig(
                id=file_id,
                parts=part_count,
                name=file_name,
            )
        return types.InputFile(
            id=file_id,
            parts=part_count,
            name=file_name,
            md5_checksum=md5_digest.hexdigest(),
        )

    async def _invoke_final_request(
        self,
        client: Any,
        request: Any,
        *,
        before_final_request: BeforeFinalRequest | None,
    ) -> Any:
        self._raise_if_operation_aborted()
        _validate_final_request_serializable(request)
        self._raise_if_operation_aborted()
        if before_final_request is not None:
            try:
                before_final_request()
            except TelegramUploadError:
                raise
            except Exception as exc:
                raise NonRetryableUploadError(
                    "failed to persist upload state before the final Telegram request: "
                    f"{sanitize_untrusted_error_text(str(exc), secrets=self._secrets)}"
                ) from exc
        self._raise_if_operation_aborted()
        self._notify_final_request_status(True)
        self._final_request_started_for_operation = True
        try:
            try:
                return await asyncio.wait_for(
                    client(request),
                    timeout=self._final_request_timeout_seconds,
                )
            except Exception as exc:
                raise _translate_exception(
                    exc,
                    final_request_started=True,
                    secrets=self._secrets,
                    context="sending the final Telegram request",
                ) from exc
        finally:
            self._notify_final_request_status(False)

    def _notify_final_request_status(self, active: bool) -> None:
        callback = self._final_request_status
        if callback is None:
            return
        try:
            callback(active)
        except Exception:
            # A terminal renderer must never alter Telegram delivery semantics.
            pass

    async def _discard_failed_client(self) -> None:
        client = self._client
        self._client = None
        self._peer = None
        if client is None:
            return
        try:
            await _disconnect_client(client)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException:
            pass
        try:
            _protect_session_files(client)
        except TelegramUploadError:
            pass

    def _drop_connection_after_failure(self) -> None:
        if self._client is None:
            return
        try:
            self._runner.run(self._discard_failed_client())
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            self._client = None
            self._peer = None
            raise
        except BaseException:
            self._client = None
            self._peer = None

    @property
    def _reply_to(self) -> types.InputReplyToMessage | None:
        if self._reply_message_id is None:
            return None
        return types.InputReplyToMessage(self._reply_message_id)


def _validate_final_request_serializable(request: Any) -> None:
    """Fail locally before persisting ``sending`` if Telethon cannot encode the request."""

    try:
        bytes(request)
    except UnicodeEncodeError as exc:
        raise NonRetryableUploadError(
            "final Telegram request contains text that cannot be encoded as UTF-8; "
            "check captions and file names; no final Telegram request was sent"
        ) from exc
    except Exception as exc:
        raise NonRetryableUploadError(
            "final Telegram request could not be serialized locally "
            f"({type(exc).__name__}); no final Telegram request was sent"
        ) from exc


def parse_send_result(
    result: Any,
    random_ids: Sequence[int],
    *,
    expected_peer_id: int,
    expect_grouped: bool,
) -> tuple[int, ...]:
    """Map public MTProto Updates back to caller random IDs without private APIs."""

    expected_random_ids = _validate_random_ids(random_ids)
    if (
        isinstance(expected_peer_id, bool)
        or not isinstance(expected_peer_id, int)
        or expected_peer_id == 0
    ):
        raise _ResponseValidationError("expected peer ID is invalid")
    if isinstance(result, types.UpdateShortSentMessage):
        if expect_grouped or len(expected_random_ids) != 1:
            raise _ResponseValidationError("UpdateShortSentMessage cannot confirm a media group")
        if not _is_valid_message_id(result.id):
            raise _ResponseValidationError("UpdateShortSentMessage contains an invalid message ID")
        return (result.id,)
    if isinstance(result, types.UpdateShort):
        updates = (result.update,)
    elif isinstance(result, (types.Updates, types.UpdatesCombined)):
        updates = tuple(result.updates)
    else:
        raise _ResponseValidationError(
            f"unexpected response type {type(result).__name__}; expected Updates"
        )

    random_to_message_id: dict[int, int] = {}
    messages_by_id: dict[int, types.Message] = {}
    for update in updates:
        if isinstance(update, types.UpdateMessageID):
            random_id = update.random_id
            message_id = update.id
            if (
                not _is_valid_message_id(message_id)
                or isinstance(random_id, bool)
                or not isinstance(random_id, int)
            ):
                raise _ResponseValidationError("invalid UpdateMessageID values")
            if random_id in random_to_message_id:
                raise _ResponseValidationError("duplicate message-ID mapping for one random ID")
            random_to_message_id[random_id] = message_id
        elif isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            message = update.message
            if not isinstance(message, types.Message) or not _is_valid_message_id(message.id):
                raise _ResponseValidationError("invalid new-message update")
            if message.id in messages_by_id:
                raise _ResponseValidationError("duplicate new-message update")
            try:
                message_peer_id = utils.get_peer_id(message.peer_id)
            except (TypeError, ValueError) as exc:
                raise _ResponseValidationError("new-message update has an invalid peer") from exc
            if message_peer_id != expected_peer_id:
                raise _ResponseValidationError("new-message update belongs to an unexpected peer")
            messages_by_id[message.id] = message

    expected_set = set(expected_random_ids)
    if set(random_to_message_id) != expected_set:
        raise _ResponseValidationError(
            "random-ID mapping count or identity does not match the request"
        )

    ordered_message_ids = tuple(
        random_to_message_id[random_id] for random_id in expected_random_ids
    )
    if len(set(ordered_message_ids)) != len(ordered_message_ids):
        raise _ResponseValidationError("multiple random IDs mapped to the same message")
    if set(messages_by_id) != set(ordered_message_ids):
        raise _ResponseValidationError("new-message count or identity does not match the request")

    ordered_messages = tuple(messages_by_id[message_id] for message_id in ordered_message_ids)
    grouped_ids = tuple(message.grouped_id for message in ordered_messages)
    if expect_grouped:
        first_grouped_id = grouped_ids[0] if grouped_ids else None
        if (
            isinstance(first_grouped_id, bool)
            or not isinstance(first_grouped_id, int)
            or first_grouped_id == 0
            or any(grouped_id != first_grouped_id for grouped_id in grouped_ids)
        ):
            raise _ResponseValidationError("media-group messages do not share one grouped_id")
    elif grouped_ids != (None,):
        raise _ResponseValidationError(
            "a single-video response unexpectedly belongs to a media group"
        )

    return ordered_message_ids


def _translate_exception(
    exc: BaseException,
    *,
    final_request_started: bool,
    secrets: Sequence[str],
    context: str,
) -> TelegramUploadError:
    if isinstance(exc, TelegramUploadError):
        return exc

    if (
        isinstance(exc, errors.UserMigrateError)
        and context == "connecting to Telegram"
        and not final_request_started
    ):
        new_dc = getattr(exc, "new_dc", None)
        dc_label = (
            f"DC {new_dc}"
            if isinstance(new_dc, int) and not isinstance(new_dc, bool) and new_dc > 0
            else "a different Telegram data center"
        )
        return NonRetryableUploadError(
            f"Telegram moved this bot session to {dc_label}. Telethon saved the new data "
            "center in the persistent session, but this login attempt ended before retrying "
            "there. Rerun the exact same upload command; do not discard the pending state. "
            "This failed attempt did not start a final Telegram send.",
            final_request_started=False,
        )

    detail = sanitize_untrusted_error_text(str(exc), secrets=secrets)
    message = f"{context} failed ({type(exc).__name__})"
    if detail:
        message = f"{message}: {detail}"

    if isinstance(exc, errors.RandomIdDuplicateError):
        return RetryableUploadError(
            "Telegram has already seen this persistent random ID and will not create a second "
            "message, but this response did not confirm the original message_id",
            outcome_uncertain=True,
            final_request_started=True,
        )

    if isinstance(
        exc,
        (
            errors.FilePart0MissingError,
            errors.FilePartMissingError,
            errors.FileReferenceEmptyError,
            errors.FileReferenceExpiredError,
        ),
    ):
        return RetryableUploadError(
            message,
            outcome_uncertain=False,
            final_request_started=final_request_started,
        )

    if isinstance(exc, errors.FloodError):
        seconds = getattr(exc, "seconds", None)
        retry_after = int(seconds) if isinstance(seconds, (int, float)) and seconds >= 0 else None
        return RetryableUploadError(
            message,
            retry_after_seconds=retry_after,
            outcome_uncertain=False,
            final_request_started=final_request_started,
        )

    if isinstance(exc, errors.RPCError):
        code = getattr(exc, "code", None)
        if isinstance(exc, (errors.ServerError, errors.TimedOutError)) or (
            isinstance(code, int) and (code >= 500 or code < 0)
        ):
            return RetryableUploadError(
                message,
                outcome_uncertain=final_request_started,
                final_request_started=final_request_started,
            )
        return NonRetryableUploadError(message, final_request_started=final_request_started)

    if isinstance(exc, (TimeoutError, ConnectionError, EOFError, OSError)):
        return RetryableUploadError(
            message,
            outcome_uncertain=final_request_started,
            final_request_started=final_request_started,
        )

    return NonRetryableUploadError(message, final_request_started=final_request_started)


def normalize_chat_id(chat_id: str | int) -> str | int:
    if isinstance(chat_id, bool):
        raise NonRetryableUploadError("chat_id must be a string or integer")
    if isinstance(chat_id, int):
        if chat_id == 0:
            raise NonRetryableUploadError("numeric chat_id must not be zero")
        return chat_id
    if not isinstance(chat_id, str) or not chat_id:
        raise NonRetryableUploadError("chat_id must be a non-empty string or integer")
    if NUMERIC_CHAT_ID_PATTERN.fullmatch(chat_id):
        numeric_chat_id = int(chat_id)
        if numeric_chat_id == 0:
            raise NonRetryableUploadError("numeric chat_id must not be zero")
        return numeric_chat_id
    if chat_id != chat_id.strip():
        raise NonRetryableUploadError("chat_id must not contain surrounding whitespace")
    username, _ = utils.parse_username(chat_id)
    if username is None:
        raise NonRetryableUploadError(
            "non-numeric chat_id must be a Telegram username or invite link; "
            "display names are not supported"
        )
    return chat_id


def _validate_upload_item(item: UploadItem) -> None:
    if not isinstance(item, UploadItem):
        raise NonRetryableUploadError("upload item has an invalid type")
    if not isinstance(item.path, Path):
        raise NonRetryableUploadError("upload item path must be a pathlib.Path")
    if not isinstance(item.caption, str):
        raise NonRetryableUploadError("upload item caption must be a string")
    if (
        isinstance(item.expected_size, bool)
        or not isinstance(item.expected_size, int)
        or item.expected_size <= 0
    ):
        raise NonRetryableUploadError("upload item expected_size must be a positive integer")
    if (
        not isinstance(item.expected_sha256, str)
        or SHA256_HEX_PATTERN.fullmatch(item.expected_sha256) is None
    ):
        raise NonRetryableUploadError(
            "upload item expected_sha256 must be a lowercase SHA-256 hex digest"
        )


def _validate_random_id(random_id: int) -> int:
    if (
        isinstance(random_id, bool)
        or not isinstance(random_id, int)
        or random_id == 0
        or not MIN_RANDOM_ID <= random_id <= MAX_RANDOM_ID
    ):
        raise NonRetryableUploadError("random_id must be a non-zero signed 64-bit integer")
    return random_id


def _validate_random_ids(random_ids: Sequence[int]) -> tuple[int, ...]:
    validated = tuple(_validate_random_id(random_id) for random_id in random_ids)
    if not validated:
        raise NonRetryableUploadError("at least one random_id is required")
    if len(set(validated)) != len(validated):
        raise NonRetryableUploadError("random_ids must be unique within an operation")
    return validated


def _is_valid_message_id(message_id: Any) -> bool:
    return not isinstance(message_id, bool) and isinstance(message_id, int) and message_id > 0


def _regular_file_size(file_handle: BinaryIO, path: Path) -> int:
    file_stat = os.fstat(file_handle.fileno())
    if not stat.S_ISREG(file_stat.st_mode):
        raise NonRetryableUploadError(f"upload path is not a regular file: {path}")
    if file_stat.st_size <= 0:
        raise NonRetryableUploadError(f"upload file is empty: {path}")
    validate_upload_file_size(path, file_stat.st_size)
    return file_stat.st_size


def _prepare_private_session_directory(session_path: Path) -> None:
    try:
        session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_status = session_path.parent.lstat()
        if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
            raise OSError("session parent is not a real directory")
        if os.name == "posix":
            session_path.parent.chmod(0o700)
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to prepare private Telegram session directory {session_path.parent}: {exc}"
        ) from exc


def _protect_session_files(client: Any) -> None:
    if os.name != "posix":
        return
    filename = getattr(getattr(client, "session", None), "filename", None)
    if not filename:
        return
    _protect_session_path_candidates(Path(filename))


def _protect_session_path_candidates(session_path: Path) -> None:
    if os.name != "posix":
        return
    candidates = (
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
    )
    for path in candidates:
        try:
            path_status = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to inspect persistent Telegram session file {path}: {exc}"
            ) from exc
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise NonRetryableUploadError(
                f"persistent Telegram session path is not a regular file: {path}"
            )
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to protect persistent Telegram session file {path}: {exc}"
            ) from exc


async def _disconnect_client(client: Any) -> None:
    await client.disconnect()


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


class _SequentialHashingReader:
    """Hash exactly the bytes Telethon consumes from one already-open file."""

    def __init__(self, file_handle: BinaryIO) -> None:
        self._file_handle = file_handle
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    @property
    def name(self) -> str:
        return str(self._file_handle.name)

    @property
    def closed(self) -> bool:
        return self._file_handle.closed

    def read(self, size: int = -1) -> bytes:
        chunk = self._file_handle.read(size)
        self._digest.update(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()
