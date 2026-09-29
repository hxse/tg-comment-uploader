"""Synchronous command facade over the shared asynchronous MTProto sender."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

from telethon import TelegramClient, types

from .errors import AppError, NonRetryableUploadError, TelegramUploadError
from .mtproto_errors import _translate_exception
from .mtproto_files import TELEGRAM_BIG_FILE_THRESHOLD_BYTES as TELEGRAM_BIG_FILE_THRESHOLD_BYTES
from .mtproto_files import UPLOAD_MAX_IN_FLIGHT_PARTS as UPLOAD_MAX_IN_FLIGHT_PARTS
from .mtproto_files import UPLOAD_PART_SIZE_BYTES as UPLOAD_PART_SIZE_BYTES
from .mtproto_messages import _send_media, _send_media_group, send_text
from .mtproto_protocol import (
    _validate_random_id,
    _validate_random_ids,
    _validate_upload_item,
)
from .mtproto_protocol import (
    normalize_chat_id as normalize_chat_id,
)
from .mtproto_protocol import (
    parse_send_result as parse_send_result,
)
from .mtproto_runtime import _cancel_and_drain_active_operation, _disconnect_client, _run_operation
from .mtproto_session import (
    _discard_failed_client,
    _ensure_connected,
    _prepare_private_session_directory,
    _protect_session_files,
    _protect_session_path_candidates,
)
from .telegram_sender import (
    TELEGRAM_INT32_MAX,
    BeforeFinalRequest,
    FinalRequestStatus,
    MediaGroupUploadProgress,
    PeerResolved,
    UploadItem,
    UploadProgress,
    parse_bot_id,
)
from .upload_contract import MEDIA_GROUP_MAX_ITEMS, MEDIA_GROUP_MIN_ITEMS

FINAL_REQUEST_TIMEOUT_SECONDS = 10 * 60
ClientFactory = Callable[..., Any]
RunnerFactory = Callable[[], asyncio.Runner]
OperationResultT = TypeVar("OperationResultT")


class MtprotoSender:
    """Own one Bot connection and event loop for either command."""

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
                _cancel_and_drain_active_operation(self)
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
                    _send_media(
                        self,
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
                _send_media_group(
                    self,
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

    def _raise_if_operation_aborted(self) -> None:
        if self._closed or self._operation_abort_requested:
            raise asyncio.CancelledError

    @property
    def _secrets(self) -> tuple[str, str]:
        return self._bot_token, self._api_hash

    def _assert_open(self) -> None:
        if self._closed:
            raise NonRetryableUploadError("Telegram sender is already closed")

    def _drop_connection_after_failure(self) -> None:
        if self._client is None:
            return
        try:
            self._runner.run(_discard_failed_client(self))
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

    def _run_operation(self, operation: Coroutine[Any, Any, OperationResultT]) -> OperationResultT:
        return _run_operation(self, operation)

    async def _ensure_connected(self) -> tuple[Any, Any]:
        return await _ensure_connected(self)

    def run_service(self, operation: Coroutine[Any, Any, OperationResultT]) -> OperationResultT:
        """Run a listener on the same owned loop as asynchronous sends."""
        self._assert_open()
        try:
            return self._run_operation(operation)
        except AppError:
            raise
        except Exception as exc:
            raise _translate_exception(
                exc,
                final_request_started=False,
                secrets=self._secrets,
                context="running the Telegram listener",
            ) from exc

    async def connect(self) -> tuple[Any, Any]:
        return await self._ensure_connected()

    async def send_prepared(
        self,
        items: Sequence[UploadItem],
        *,
        random_ids: Sequence[int],
        before_final_request: BeforeFinalRequest,
        progress_callback: MediaGroupUploadProgress | None = None,
    ) -> tuple[int, ...]:
        """Shared async entry for downloaded files; never forwards source media IDs."""
        frozen = tuple(items)
        ids = _validate_random_ids(random_ids)
        if not 1 <= len(frozen) <= MEDIA_GROUP_MAX_ITEMS or len(ids) != len(frozen):
            raise NonRetryableUploadError("invalid prepared message count")
        for item in frozen:
            _validate_upload_item(item)
        self._final_request_started_for_operation = False
        if len(frozen) == 1:
            return (
                await _send_media(
                    self,
                    frozen[0],
                    random_id=ids[0],
                    progress_callback=(lambda sent, total: progress_callback(0, sent, total))
                    if progress_callback is not None
                    else None,
                    before_final_request=before_final_request,
                ),
            )
        return await _send_media_group(
            self,
            frozen,
            random_ids=ids,
            progress_callback=progress_callback,
            before_final_request=before_final_request,
        )

    async def send_text(
        self,
        text: str,
        *,
        entities: tuple[Any, ...],
        random_id: int,
        before_final_request: BeforeFinalRequest,
    ) -> tuple[int, ...]:
        self._final_request_started_for_operation = False
        return await send_text(
            self,
            text,
            entities=entities,
            random_id=_validate_random_id(random_id),
            before_final_request=before_final_request,
        )
