from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors, functions, types

from tg_comment_uploader.cli import termination_as_interrupt
from tg_comment_uploader.errors import NonRetryableUploadError, RetryableUploadError
from tg_comment_uploader.mtproto_sender import (
    TELEGRAM_BIG_FILE_THRESHOLD_BYTES,
    UPLOAD_MAX_IN_FLIGHT_PARTS,
    UPLOAD_PART_SIZE_BYTES,
    MtprotoSender,
)
from tg_comment_uploader.telegram_sender import SAFE_UPLOAD_LIMIT_BYTES, UploadItem, parse_bot_id


def make_message(message_id: int, *, grouped_id: int | None = None) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(123),
        date=None,
        message="",
        grouped_id=grouped_id,
    )


def make_send_result(random_ids: tuple[int, ...], *, grouped_id: int | None) -> types.Updates:
    updates: list[Any] = []
    for offset, random_id in enumerate(random_ids, start=1):
        message_id = 100 + offset
        updates.extend(
            (
                types.UpdateMessageID(id=message_id, random_id=random_id),
                types.UpdateNewChannelMessage(
                    message=make_message(message_id, grouped_id=grouped_id),
                    pts=offset,
                    pts_count=1,
                ),
            )
        )
    return types.Updates(updates=updates, users=[], chats=[], date=None, seq=1)


def make_document(document_id: int) -> types.Document:
    return types.Document(
        id=document_id,
        access_hash=document_id + 1,
        file_reference=b"reference",
        date=datetime.now(UTC),
        mime_type="video/mp4",
        size=5,
        dc_id=2,
        attributes=[],
    )


def upload_item(path: Path, caption: str = "") -> UploadItem:
    size = path.stat().st_size
    digest = (
        hashlib.sha256(path.read_bytes()).hexdigest()
        if size <= SAFE_UPLOAD_LIMIT_BYTES
        else "0" * 64
    )
    return UploadItem(path, caption, expected_size=size, expected_sha256=digest)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (None, "must be a string"),
        ("missing-separator", "valid bot ID prefix"),
        ("0:secret", "non-positive bot ID"),
    ],
)
def test_parse_bot_id_rejects_invalid_tokens(token: object, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        parse_bot_id(token)


def test_parse_bot_id_returns_positive_prefix() -> None:
    assert parse_bot_id("123456:secret") == 123456


class FakeClient:
    def __init__(
        self,
        *args: object,
        bot_id: int = 123456,
        failure: tuple[str, BaseException] | None = None,
        **kwargs: object,
    ) -> None:
        self.args = args
        self.kwargs = kwargs
        self.bot_id = bot_id
        self.failure = failure
        self.session = SimpleNamespace(filename=None)
        self.started_with: str | None = None
        self.entity_argument: object | None = None
        self.upload_calls: list[
            functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest
        ] = []
        self.requests: list[object] = []
        self.disconnected = False
        self.start_calls = 0
        self._next_document_id = 1000

    async def start(self, *, bot_token: str) -> FakeClient:
        self.start_calls += 1
        self.started_with = bot_token
        self._raise_if("start")
        return self

    async def get_me(self) -> object:
        self._raise_if("get_me")
        return SimpleNamespace(id=self.bot_id, bot=True)

    async def get_input_entity(self, entity: object) -> types.InputPeerChannel:
        self.entity_argument = entity
        self._raise_if("get_input_entity")
        return types.InputPeerChannel(channel_id=123, access_hash=456)

    async def _save_part(
        self,
        request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
    ) -> bool:
        self._raise_if("upload_file")
        self.upload_calls.append(request)
        return True

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if isinstance(
            request,
            (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest),
        ):
            return await self._save_part(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            self._raise_if("upload_media")
            self._next_document_id += 1
            return types.MessageMediaDocument(document=make_document(self._next_document_id))
        if isinstance(request, functions.messages.SendMediaRequest):
            self._raise_if("send_final")
            return make_send_result((request.random_id,), grouped_id=None)
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            self._raise_if("send_final")
            return make_send_result(
                tuple(media.random_id for media in request.multi_media),
                grouped_id=777,
            )
        raise AssertionError(f"unexpected request: {request!r}")

    async def disconnect(self) -> None:
        self.disconnected = True

    def is_connected(self) -> bool:
        return not self.disconnected

    def _raise_if(self, phase: str) -> None:
        if self.failure is not None and self.failure[0] == phase:
            raise self.failure[1]


def make_sender(
    tmp_path: Path,
    *,
    client: FakeClient,
    final_status: list[bool] | None = None,
    peer_resolved: Callable[[int], None] | None = None,
    final_request_timeout_seconds: float = 600,
    supports_streaming: bool = True,
) -> MtprotoSender:
    status = final_status if final_status is not None else []
    return MtprotoSender(
        api_id=12345,
        api_hash="api-secret",
        bot_token="123456:bot-secret",
        session_path=tmp_path / "state" / "bot.session",
        chat_id="-1001234567890",
        reply_message_id=12345,
        supports_streaming=supports_streaming,
        final_request_status=status.append,
        peer_resolved=peer_resolved,
        final_request_timeout_seconds=final_request_timeout_seconds,
        client_factory=lambda *args, **kwargs: _capture_factory(client, args, kwargs),
    )


def _capture_factory(
    client: FakeClient,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> FakeClient:
    client.args = args
    client.kwargs = kwargs
    return client


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_id": 2_147_483_648}, "api_id.*signed 32-bit"),
        ({"reply_message_id": 2_147_483_648}, "reply_message_id.*signed 32-bit"),
    ],
)
def test_sender_rejects_values_telethon_cannot_serialize_as_int32(
    tmp_path: Path,
    overrides: dict[str, int],
    message: str,
) -> None:
    parameters: dict[str, Any] = {
        "api_id": 12345,
        "api_hash": "api-secret",
        "bot_token": "123456:bot-secret",
        "session_path": tmp_path / "state" / "bot.session",
        "chat_id": "-1001234567890",
        "reply_message_id": 12345,
        "supports_streaming": True,
    }
    parameters.update(overrides)

    with pytest.raises(NonRetryableUploadError, match=message):
        MtprotoSender(**parameters)


def test_send_video_uses_one_fd_raw_request_and_persistent_random_id(tmp_path: Path) -> None:
    path = tmp_path / "sample video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    final_status: list[bool] = []
    resolved_peers: list[int] = []
    progress: list[tuple[int, int]] = []

    with make_sender(
        tmp_path,
        client=client,
        final_status=final_status,
        peer_resolved=resolved_peers.append,
    ) as sender:
        message_id = sender.send_video(
            upload_item(path, "caption"),
            random_id=-9001,
            progress_callback=lambda sent, total: progress.append((sent, total)),
        )

    assert message_id == 101
    assert client.started_with == "123456:bot-secret"
    assert client.entity_argument == -1001234567890
    assert client.kwargs["request_retries"] == 0
    assert client.kwargs["connection_retries"] == 0
    assert client.kwargs["auto_reconnect"] is False
    assert client.kwargs["flood_sleep_threshold"] == 0
    assert len(client.upload_calls) == 1
    upload_part = client.upload_calls[0]
    assert isinstance(upload_part, functions.upload.SaveFilePartRequest)
    assert upload_part.file_part == 0
    assert upload_part.bytes == b"video"
    assert progress == [(0, 5), (5, 5)]
    assert final_status == [True, False]
    assert resolved_peers == [-1000000000123]
    assert client.disconnected is True

    request = next(
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert request.random_id == -9001
    assert request.message == "caption"
    assert request.entities is None
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.reply_to_msg_id == 12345
    assert isinstance(request.media, types.InputMediaUploadedDocument)
    assert request.media.nosound_video is True
    assert isinstance(request.media.file, types.InputFile)
    assert request.media.file.id == upload_part.file_id
    assert request.media.file.parts == 1
    assert request.media.file.name == "sample video.mp4"
    video_attributes = [
        attribute
        for attribute in request.media.attributes
        if isinstance(attribute, types.DocumentAttributeVideo)
    ]
    assert len(video_attributes) == 1
    assert video_attributes[0].supports_streaming is True


def test_part_upload_uses_bounded_pipeline_and_monotonic_acknowledged_progress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large video.mp4"
    file_size = TELEGRAM_BIG_FILE_THRESHOLD_BYTES + 1
    path.write_bytes(b"x" * file_size)

    class WindowedPartClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.active_parts = 0
            self.max_active_parts = 0
            self.started_parts: list[int] = []
            self.completed_parts: list[int] = []
            self.initial_window_ready = asyncio.Event()

        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            self.active_parts += 1
            self.max_active_parts = max(self.max_active_parts, self.active_parts)
            self.started_parts.append(request.file_part)
            if self.active_parts == UPLOAD_MAX_IN_FLIGHT_PARTS:
                self.initial_window_ready.set()
            try:
                await self.initial_window_ready.wait()
                # Reverse the completion order within the initial window so the
                # progress contract does not accidentally depend on part order.
                delay_slot = (UPLOAD_MAX_IN_FLIGHT_PARTS - 1) - (
                    request.file_part % UPLOAD_MAX_IN_FLIGHT_PARTS
                )
                await asyncio.sleep(delay_slot / 10_000)
                result = await super()._save_part(request)
                self.completed_parts.append(request.file_part)
                return result
            finally:
                self.active_parts -= 1

    client = WindowedPartClient()
    progress: list[tuple[int, int]] = []

    with make_sender(tmp_path, client=client) as sender:
        assert (
            sender.send_video(
                upload_item(path),
                random_id=1,
                progress_callback=lambda sent, total: progress.append((sent, total)),
            )
            == 101
        )

    expected_parts = (file_size + UPLOAD_PART_SIZE_BYTES - 1) // UPLOAD_PART_SIZE_BYTES
    assert len(client.upload_calls) == expected_parts
    assert client.max_active_parts == UPLOAD_MAX_IN_FLIGHT_PARTS
    assert client.completed_parts != sorted(client.completed_parts)
    assert {request.file_id for request in client.upload_calls} == {client.upload_calls[0].file_id}
    big_upload_calls = [
        request
        for request in client.upload_calls
        if isinstance(request, functions.upload.SaveBigFilePartRequest)
    ]
    assert len(big_upload_calls) == expected_parts
    assert {request.file_part for request in client.upload_calls} == set(range(expected_parts))
    assert {request.file_total_parts for request in big_upload_calls} == {expected_parts}
    assert all(0 < len(request.bytes) <= UPLOAD_PART_SIZE_BYTES for request in client.upload_calls)
    part_sizes = {request.file_part: len(request.bytes) for request in client.upload_calls}
    assert all(part_sizes[index] == UPLOAD_PART_SIZE_BYTES for index in range(expected_parts - 1))
    assert part_sizes[expected_parts - 1] == file_size % UPLOAD_PART_SIZE_BYTES
    assert [sent for sent, _ in progress] == sorted(sent for sent, _ in progress)
    assert all(total == file_size for _, total in progress)
    assert progress[-1] == (file_size, file_size)

    final_request = next(
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert isinstance(final_request.media, types.InputMediaUploadedDocument)
    assert isinstance(final_request.media.file, types.InputFileBig)
    assert final_request.media.file.parts == expected_parts


def test_part_failure_cancels_and_drains_the_remaining_window_before_final_send(
    tmp_path: Path,
) -> None:
    path = tmp_path / "failing video.mp4"
    path.write_bytes(b"x" * ((UPLOAD_MAX_IN_FLIGHT_PARTS + 1) * UPLOAD_PART_SIZE_BYTES))

    class FailingWindowClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.active_parts = 0
            self.started_parts: set[int] = set()
            self.cancelled_parts: set[int] = set()
            self.initial_window_ready = asyncio.Event()
            self.never_complete = asyncio.Event()

        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            self.active_parts += 1
            self.started_parts.add(request.file_part)
            if len(self.started_parts) == UPLOAD_MAX_IN_FLIGHT_PARTS:
                self.initial_window_ready.set()
            try:
                await self.initial_window_ready.wait()
                if request.file_part == 0:
                    raise ConnectionResetError("part connection lost")
                await self.never_complete.wait()
                raise AssertionError("blocked upload part unexpectedly resumed")
            except asyncio.CancelledError:
                self.cancelled_parts.add(request.file_part)
                raise
            finally:
                self.active_parts -= 1

    client = FailingWindowClient()
    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    assert caught.value.outcome_uncertain is False
    assert caught.value.final_request_started is False
    assert client.started_parts == set(range(UPLOAD_MAX_IN_FLIGHT_PARTS))
    assert client.cancelled_parts == set(range(1, UPLOAD_MAX_IN_FLIGHT_PARTS))
    assert client.active_parts == 0
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_transport_cancelled_part_is_safe_retryable_and_reconnects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class DisconnectedPartClient(FakeClient):
        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            self.upload_calls.append(request)
            self.disconnected = True
            raise asyncio.CancelledError

    first_client = DisconnectedPartClient()
    second_client = FakeClient()
    clients = iter((first_client, second_client))
    factory_calls: list[FakeClient] = []

    def client_factory(*args: object, **kwargs: object) -> FakeClient:
        client = next(clients)
        factory_calls.append(_capture_factory(client, args, kwargs))
        return client

    sender = MtprotoSender(
        api_id=12345,
        api_hash="api-secret",
        bot_token="123456:bot-secret",
        session_path=tmp_path / "state" / "bot.session",
        chat_id="-1001234567890",
        reply_message_id=12345,
        supports_streaming=True,
        client_factory=client_factory,
    )
    before_final_calls: list[bool] = []
    item = upload_item(path)

    with sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(
                item,
                random_id=991,
                before_final_request=lambda: before_final_calls.append(True),
            )

        assert caught.value.outcome_uncertain is False
        assert caught.value.final_request_started is False
        assert "before the final send" in str(caught.value)
        assert "retrying from the beginning is safe" in str(caught.value)
        assert before_final_calls == []
        assert first_client.disconnected is True
        assert not any(
            isinstance(request, functions.messages.SendMediaRequest)
            for request in first_client.requests
        )

        assert (
            sender.send_video(
                item,
                random_id=991,
                before_final_request=lambda: before_final_calls.append(True),
            )
            == 101
        )

    assert factory_calls == [first_client, second_client]
    assert before_final_calls == [True]
    final_request = next(
        request
        for request in second_client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert final_request.random_id == 991


def test_exact_big_file_boundary_uses_small_file_protocol_and_md5(tmp_path: Path) -> None:
    path = tmp_path / "ten mib.mp4"
    payload = b"x" * TELEGRAM_BIG_FILE_THRESHOLD_BYTES
    path.write_bytes(payload)
    client = FakeClient()

    with make_sender(tmp_path, client=client) as sender:
        assert sender.send_video(upload_item(path), random_id=1) == 101

    assert all(
        isinstance(request, functions.upload.SaveFilePartRequest) for request in client.upload_calls
    )
    final_request = next(
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert isinstance(final_request.media, types.InputMediaUploadedDocument)
    assert isinstance(final_request.media.file, types.InputFile)
    assert (
        final_request.media.file.md5_checksum
        == hashlib.md5(
            payload,
            usedforsecurity=False,
        ).hexdigest()
    )


def test_media_group_materializes_each_file_then_sends_one_raw_album(tmp_path: Path) -> None:
    paths = (tmp_path / "part 0001.mp4", tmp_path / "part 0002.mp4")
    for path in paths:
        path.write_bytes(b"video")
    client = FakeClient()
    progress: list[tuple[int, int, int]] = []

    with make_sender(tmp_path, client=client) as sender:
        message_ids = sender.send_media_group(
            tuple(upload_item(path, f"caption {index}") for index, path in enumerate(paths)),
            random_ids=(111, -222),
            progress_callback=lambda index, sent, total: progress.append((index, sent, total)),
        )

    assert message_ids == (101, 102)
    assert progress == [(0, 0, 5), (0, 5, 5), (1, 0, 5), (1, 5, 5)]
    high_level_requests = [
        request
        for request in client.requests
        if not isinstance(
            request,
            (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest),
        )
    ]
    assert [type(request) for request in high_level_requests] == [
        functions.messages.UploadMediaRequest,
        functions.messages.UploadMediaRequest,
        functions.messages.SendMultiMediaRequest,
    ]
    album = high_level_requests[-1]
    assert isinstance(album, functions.messages.SendMultiMediaRequest)
    assert isinstance(album.reply_to, types.InputReplyToMessage)
    assert album.reply_to.reply_to_msg_id == 12345
    assert [media.random_id for media in album.multi_media] == [111, -222]
    assert [media.message for media in album.multi_media] == ["caption 0", "caption 1"]
    assert all(isinstance(media.media, types.InputMediaDocument) for media in album.multi_media)


def test_session_identity_must_match_token_prefix(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient(bot_id=999999)

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="different bot token"):
            sender.send_video(upload_item(path), random_id=1)

    assert client.upload_calls == []


def test_existing_session_and_sqlite_sidecars_are_tightened_before_login(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode bits are not available")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    session_path = tmp_path / "state" / "bot.session"
    session_path.parent.mkdir(parents=True)
    session_files = (
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
    )
    for session_file in session_files:
        session_file.write_bytes(b"state")
        session_file.chmod(0o644)
    client = FakeClient()
    client.session.filename = str(session_path)

    with make_sender(tmp_path, client=client) as sender:
        assert sender.send_video(upload_item(path), random_id=1) == 101

    assert all(session_file.stat().st_mode & 0o777 == 0o600 for session_file in session_files)


def test_random_id_duplicate_is_uncertain_and_reuses_caller_id(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    duplicate = errors.RandomIdDuplicateError(
        functions.messages.SendMediaRequest(
            peer=types.InputPeerSelf(),
            media=types.InputMediaEmpty(),
            message="",
            random_id=44,
        )
    )
    client = FakeClient(failure=("send_final", duplicate))

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=44)

    assert caught.value.outcome_uncertain is True
    assert caught.value.final_request_started is True
    assert "will not create a second message" in str(caught.value)


def test_final_network_failure_is_uncertain_but_upload_failure_is_not(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    final_client = FakeClient(failure=("send_final", ConnectionResetError("lost")))
    with make_sender(tmp_path, client=final_client) as sender:
        with pytest.raises(RetryableUploadError) as final_error:
            sender.send_video(upload_item(path), random_id=1)
    assert final_error.value.outcome_uncertain is True
    assert final_error.value.final_request_started is True

    upload_client = FakeClient(failure=("upload_file", ConnectionResetError("lost")))
    with make_sender(tmp_path, client=upload_client) as sender:
        with pytest.raises(RetryableUploadError) as upload_error:
            sender.send_video(upload_item(path), random_id=1)
    assert upload_error.value.outcome_uncertain is False
    assert upload_error.value.final_request_started is False


def test_connection_error_redacts_config_secrets(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient(
        failure=("start", RuntimeError("123456:bot-secret\napi-secret")),
    )

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    rendered = str(caught.value)
    assert "bot-secret" not in rendered
    assert "api-secret" not in rendered
    assert "\\n" in rendered
    assert client.disconnected is True


def test_failed_start_disconnected_future_is_observed_before_safe_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class TrackingFuture(asyncio.Future[None]):
        def __init__(self) -> None:
            super().__init__()
            self.exception_calls = 0

        def exception(self) -> BaseException | None:
            self.exception_calls += 1
            return super().exception()

    class FailedStartClient:
        def __init__(self) -> None:
            self.session = SimpleNamespace(filename=None)
            self.disconnected: TrackingFuture | None = None
            self.disconnect_calls = 0

        async def start(self, *, bot_token: str) -> FailedStartClient:
            del bot_token
            failure = asyncio.IncompleteReadError(partial=b"", expected=8)
            self.disconnected = TrackingFuture()
            self.disconnected.set_exception(failure)
            raise failure

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

        def is_connected(self) -> bool:
            return False

    first_client = FailedStartClient()
    second_client = FakeClient()
    clients = iter((first_client, second_client))

    sender = MtprotoSender(
        api_id=12345,
        api_hash="api-secret",
        bot_token="123456:bot-secret",
        session_path=tmp_path / "state" / "bot.session",
        chat_id="-1001234567890",
        reply_message_id=12345,
        supports_streaming=True,
        client_factory=lambda *args, **kwargs: next(clients),
    )
    item = upload_item(path)

    with sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(item, random_id=5)

        assert caught.value.outcome_uncertain is False
        assert caught.value.final_request_started is False
        assert first_client.disconnect_calls == 1
        assert first_client.disconnected is not None
        assert first_client.disconnected.exception_calls >= 1

        assert sender.send_video(item, random_id=5) == 101


def test_uncommon_extension_still_sends_as_video_and_preserves_streaming_false(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.payload"
    path.write_bytes(b"video")
    client = FakeClient()

    with make_sender(tmp_path, client=client, supports_streaming=False) as sender:
        assert sender.send_video(upload_item(path), random_id=1) == 101

    request = next(
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert isinstance(request.media, types.InputMediaUploadedDocument)
    video_attributes = [
        attribute
        for attribute in request.media.attributes
        if isinstance(attribute, types.DocumentAttributeVideo)
    ]
    assert len(video_attributes) == 1
    assert video_attributes[0].supports_streaming is False


@pytest.mark.parametrize("replacement", [b"tiny", b"video-grown"])
def test_upload_rejects_file_mutation_on_same_open_fd(
    tmp_path: Path,
    replacement: bytes,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class MutatingClient(FakeClient):
        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            path.write_bytes(replacement)
            return await super()._save_part(request)

    client = MutatingClient()
    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="changed while reading"):
            sender.send_video(upload_item(path), random_id=1)

    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_sender_enforces_global_safe_size_limit_before_upload(tmp_path: Path) -> None:
    path = tmp_path / "oversized.mp4"
    with path.open("wb") as file_handle:
        file_handle.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    client = FakeClient()

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="limit=2,097,152,000"):
            sender.send_video(upload_item(path), random_id=1)

    assert client.upload_calls == []


def test_peer_binding_failure_stops_before_upload_and_disconnects(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()

    def fail_to_bind(peer_id: int) -> None:
        del peer_id
        raise RuntimeError("state write failed")

    with make_sender(tmp_path, client=client, peer_resolved=fail_to_bind) as sender:
        with pytest.raises(NonRetryableUploadError, match="state write failed"):
            sender.send_video(upload_item(path), random_id=1)

    assert client.upload_calls == []
    assert client.requests == []
    assert client.disconnected is True


def test_retryable_failure_discards_connection_before_next_attempt(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient(failure=("send_final", ConnectionResetError("lost")))

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError):
            sender.send_video(upload_item(path), random_id=5)

        assert client.disconnected is True
        client.failure = None
        assert sender.send_video(upload_item(path), random_id=5) == 101

    assert client.start_calls == 2


def test_path_replacement_cannot_change_the_already_open_upload(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class ReplacingClient(FakeClient):
        uploaded_data: bytes | None = None

        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            path.unlink()
            path.write_bytes(b"other")
            self.uploaded_data = request.bytes
            return await super()._save_part(request)

    client = ReplacingClient()
    with make_sender(tmp_path, client=client) as sender:
        assert sender.send_video(upload_item(path), random_id=9) == 101

    assert client.uploaded_data == b"video"
    assert path.read_bytes() == b"other"


def test_flood_wait_and_bad_request_have_distinct_retry_semantics(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    request = functions.messages.SendMediaRequest(
        peer=types.InputPeerSelf(),
        media=types.InputMediaEmpty(),
        message="",
        random_id=1,
    )

    flood_client = FakeClient(
        failure=("send_final", errors.FloodWaitError(request=request, capture=23))
    )
    with make_sender(tmp_path, client=flood_client) as sender:
        with pytest.raises(RetryableUploadError) as flood_error:
            sender.send_video(upload_item(path), random_id=1)
    assert flood_error.value.retry_after_seconds == 23
    assert flood_error.value.outcome_uncertain is False
    assert flood_error.value.final_request_started is True

    bad_request_client = FakeClient(
        failure=("send_final", errors.BadRequestError(request, "MEDIA_INVALID"))
    )
    with make_sender(tmp_path, client=bad_request_client) as sender:
        with pytest.raises(NonRetryableUploadError) as bad_request_error:
            sender.send_video(upload_item(path), random_id=1)
    assert bad_request_error.value.final_request_started is True


@pytest.mark.parametrize(
    "failure",
    [
        errors.FilePart0MissingError(functions.upload.SaveBigFilePartRequest(1, 0, 1, b"part")),
        errors.FilePartMissingError(
            functions.upload.SaveBigFilePartRequest(1, 0, 1, b"part"),
            capture=0,
        ),
        errors.FileReferenceExpiredError(
            functions.messages.SendMediaRequest(
                peer=types.InputPeerSelf(),
                media=types.InputMediaEmpty(),
                message="",
                random_id=1,
            )
        ),
    ],
)
def test_missing_upload_part_or_expired_reference_is_retryable(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient(failure=("send_final", failure))

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    assert caught.value.outcome_uncertain is False
    assert caught.value.final_request_started is True


@pytest.mark.parametrize(
    ("failure", "outcome_uncertain"),
    [
        (
            errors.ServerError(
                functions.messages.SendMediaRequest(
                    peer=types.InputPeerSelf(),
                    media=types.InputMediaEmpty(),
                    message="",
                    random_id=1,
                ),
                "INTERNAL",
                code=500,
            ),
            True,
        ),
        (
            errors.TimedOutError(
                functions.messages.SendMediaRequest(
                    peer=types.InputPeerSelf(),
                    media=types.InputMediaEmpty(),
                    message="",
                    random_id=1,
                ),
                "TIMEOUT",
                code=-503,
            ),
            True,
        ),
    ],
)
def test_server_and_timeout_errors_are_retryable(
    tmp_path: Path,
    failure: BaseException,
    outcome_uncertain: bool,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient(failure=("send_final", failure))

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    assert caught.value.outcome_uncertain is outcome_uncertain
    assert caught.value.final_request_started is True


@pytest.mark.parametrize(
    "failure_type",
    [
        lambda request: errors.PhoneMigrateError(request, capture=2),
        lambda request: errors.NetworkMigrateError(request, capture=2),
        lambda request: errors.UserMigrateError(request, capture=2),
        lambda request: errors.FileMigrateError(request, capture=2),
        lambda request: errors.StatsMigrateError(request, capture=2),
    ],
)
def test_dc_migration_errors_stop_instead_of_claiming_automatic_recovery(
    tmp_path: Path,
    failure_type: Callable[[Any], errors.InvalidDCError],
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    failure = failure_type(
        functions.messages.SendMediaRequest(
            peer=types.InputPeerSelf(),
            media=types.InputMediaEmpty(),
            message="",
            random_id=1,
        )
    )
    client = FakeClient(failure=("send_final", failure))

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    assert caught.value.final_request_started is True


def test_user_migrate_during_bot_login_gives_exact_safe_recovery_instructions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    request = functions.auth.ImportBotAuthorizationRequest(
        flags=0,
        api_id=12345,
        api_hash="api-secret",
        bot_auth_token="123456:bot-secret",
    )
    client = FakeClient(
        failure=("start", errors.UserMigrateError(request, capture=5)),
    )

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    message = str(caught.value)
    assert "DC 5" in message
    assert "persistent session" in message
    assert "Rerun the exact same upload command" in message
    assert "do not discard the pending state" in message
    assert "did not start a final Telegram send" in message
    assert caught.value.final_request_started is False
    assert client.upload_calls == []


def test_cancellation_preserves_final_boundary_and_stops_status_callback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class CancelledFinalClient(FakeClient):
        async def __call__(self, request: object) -> object:
            if isinstance(request, functions.messages.SendMediaRequest):
                raise asyncio.CancelledError
            return await super().__call__(request)

    client = CancelledFinalClient()
    final_status: list[bool] = []
    with make_sender(tmp_path, client=client, final_status=final_status) as sender:
        with pytest.raises(asyncio.CancelledError):
            sender.send_video(upload_item(path), random_id=1)

    assert final_status == [True, False]
    assert client.disconnected is True


def test_disconnected_final_request_cancellation_is_uncertain_retryable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class DisconnectedFinalClient(FakeClient):
        async def __call__(self, request: object) -> object:
            if isinstance(request, functions.messages.SendMediaRequest):
                self.requests.append(request)
                self.disconnected = True
                raise asyncio.CancelledError
            return await super().__call__(request)

    client = DisconnectedFinalClient()
    final_status: list[bool] = []
    before_final_calls: list[bool] = []
    with make_sender(tmp_path, client=client, final_status=final_status) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(
                upload_item(path),
                random_id=1,
                before_final_request=lambda: before_final_calls.append(True),
            )

    assert caught.value.outcome_uncertain is True
    assert caught.value.final_request_started is True
    assert "delivery could not be confirmed" in str(caught.value)
    assert before_final_calls == [True]
    assert final_status == [True, False]
    assert client.disconnected is True


def test_real_runner_sigterm_cancels_and_drains_upload_before_disconnect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * (UPLOAD_MAX_IN_FLIGHT_PARTS * UPLOAD_PART_SIZE_BYTES))

    class SigtermDuringUploadClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        async def _save_part(
            self,
            request: functions.upload.SaveFilePartRequest | functions.upload.SaveBigFilePartRequest,
        ) -> bool:
            uploaded = await super()._save_part(request)
            self.events.append(f"part-started-{request.file_part}")
            if len(self.upload_calls) == UPLOAD_MAX_IN_FLIGHT_PARTS:
                self.events.append("sigterm-scheduled")
                # Even a simultaneous transport-close indication must not
                # turn an explicit process termination into a network retry.
                self.disconnected = True
                asyncio.get_running_loop().call_soon(
                    signal.raise_signal,
                    signal.SIGTERM,
                )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.events.append(f"part-cancelled-{request.file_part}")
                raise
            self.events.append(f"part-resumed-{request.file_part}")
            return uploaded

        async def __call__(self, request: object) -> object:
            if isinstance(
                request,
                (functions.messages.SendMediaRequest, functions.messages.SendMultiMediaRequest),
            ):
                self.events.append("final-request")
            return await super().__call__(request)

        async def disconnect(self) -> None:
            self.events.append("disconnect-started")
            await asyncio.sleep(0)
            self.disconnected = True
            self.events.append("disconnect-finished")

    client = SigtermDuringUploadClient()
    before_final_calls: list[bool] = []

    with termination_as_interrupt():
        with pytest.raises(KeyboardInterrupt):
            with make_sender(tmp_path, client=client) as sender:
                sender.send_video(
                    upload_item(path),
                    random_id=1,
                    before_final_request=lambda: before_final_calls.append(True),
                )

    assert before_final_calls == []
    assert "final-request" not in client.events
    assert not any(event.startswith("part-resumed-") for event in client.events)
    assert {
        int(event.removeprefix("part-started-"))
        for event in client.events
        if event.startswith("part-started-")
    } == set(range(UPLOAD_MAX_IN_FLIGHT_PARTS))
    cancelled_event_indexes = [
        index for index, event in enumerate(client.events) if event.startswith("part-cancelled-")
    ]
    assert len(cancelled_event_indexes) == UPLOAD_MAX_IN_FLIGHT_PARTS
    assert client.events.count("sigterm-scheduled") == 1
    assert client.events.index("disconnect-started") > max(cancelled_event_indexes)
    assert client.events[-2:] == ["disconnect-started", "disconnect-finished"]
    assert client.disconnected is True


def test_state_transition_failure_prevents_final_request(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()

    def fail_state_transition() -> None:
        raise OSError("disk full")

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="persist upload state") as caught:
            sender.send_video(
                upload_item(path),
                random_id=1,
                before_final_request=fail_state_transition,
            )

    assert caught.value.final_request_started is False
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_video_tl_serialization_failure_precedes_sending_transition(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    before_final_calls: list[bool] = []
    final_status: list[bool] = []

    with make_sender(tmp_path, client=client, final_status=final_status) as sender:
        with pytest.raises(NonRetryableUploadError, match="cannot be encoded as UTF-8") as caught:
            sender.send_video(
                upload_item(path, "invalid surrogate: \ud800"),
                random_id=1,
                before_final_request=lambda: before_final_calls.append(True),
            )

    assert caught.value.final_request_started is False
    assert before_final_calls == []
    assert final_status == []
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_media_group_tl_serialization_failure_precedes_sending_transition(
    tmp_path: Path,
) -> None:
    paths = (tmp_path / "part-1.mp4", tmp_path / "part-2.mp4")
    for path in paths:
        path.write_bytes(b"video")
    client = FakeClient()
    before_final_calls: list[bool] = []
    final_status: list[bool] = []

    items = (
        upload_item(paths[0], "valid caption"),
        upload_item(paths[1], "invalid surrogate: \udfff"),
    )
    with make_sender(tmp_path, client=client, final_status=final_status) as sender:
        with pytest.raises(NonRetryableUploadError, match="cannot be encoded as UTF-8") as caught:
            sender.send_media_group(
                items,
                random_ids=(1, 2),
                before_final_request=lambda: before_final_calls.append(True),
            )

    assert caught.value.final_request_started is False
    assert before_final_calls == []
    assert final_status == []
    assert (
        sum(
            isinstance(request, functions.messages.UploadMediaRequest)
            for request in client.requests
        )
        == 2
    )
    assert not any(
        isinstance(request, functions.messages.SendMultiMediaRequest) for request in client.requests
    )


def test_final_request_timeout_is_uncertain_retryable_and_stops_wait_indicator(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class HangingFinalClient(FakeClient):
        async def __call__(self, request: object) -> object:
            self.requests.append(request)
            if isinstance(request, functions.messages.SendMediaRequest):
                await asyncio.sleep(60)
            return await super().__call__(request)

    client = HangingFinalClient()
    final_status: list[bool] = []
    with make_sender(
        tmp_path,
        client=client,
        final_status=final_status,
        final_request_timeout_seconds=0.01,
    ) as sender:
        with pytest.raises(RetryableUploadError) as caught:
            sender.send_video(upload_item(path), random_id=1)

    assert caught.value.outcome_uncertain is True
    assert caught.value.final_request_started is True
    assert final_status == [True, False]
    assert client.disconnected is True


def test_unresolvable_numeric_peer_stops_without_bot_forbidden_dialog_refresh(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class AlwaysMissing(FakeClient):
        entity_calls = 0

        async def get_input_entity(self, entity: object) -> types.InputPeerChannel:
            self.entity_calls += 1
            self.entity_argument = entity
            raise ValueError("missing access hash")

    client = AlwaysMissing()
    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="make sure the bot can access"):
            sender.send_video(upload_item(path), random_id=1)

    assert client.entity_calls == 1
    assert client.upload_calls == []
    assert client.disconnected is True


def test_username_peer_uses_a_fresh_public_lookup_instead_of_session_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    class UsernameClient(FakeClient):
        fresh_lookups: list[str] = []

        async def get_entity(self, entity: str) -> types.InputPeerChannel:
            self.fresh_lookups.append(entity)
            return types.InputPeerChannel(channel_id=123, access_hash=456)

        async def get_input_entity(self, entity: object) -> types.InputPeerChannel:
            raise AssertionError("username resolution must not trust the session cache")

    client = UsernameClient()
    sender = MtprotoSender(
        api_id=12345,
        api_hash="api-secret",
        bot_token="123456:bot-secret",
        session_path=tmp_path / "state" / "bot.session",
        chat_id="@current_channel_name",
        reply_message_id=None,
        supports_streaming=True,
        client_factory=lambda *args, **kwargs: _capture_factory(client, args, kwargs),
    )
    with sender:
        assert sender.send_video(upload_item(path), random_id=1) == 101

    assert client.fresh_lookups == ["@current_channel_name"]
    request = next(
        request
        for request in client.requests
        if isinstance(request, functions.messages.SendMediaRequest)
    )
    assert request.reply_to is None


def test_same_size_preflight_content_mismatch_never_starts_final_request(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    original = b"video"
    path.write_bytes(original)
    expected_sha256 = hashlib.sha256(original).hexdigest()
    path.write_bytes(b"other")
    client = FakeClient()
    before_final_calls: list[bool] = []

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="preflight identity") as caught:
            sender.send_video(
                UploadItem(
                    path,
                    "",
                    expected_size=len(original),
                    expected_sha256=expected_sha256,
                ),
                random_id=1,
                before_final_request=lambda: before_final_calls.append(True),
            )

    assert caught.value.final_request_started is False
    assert before_final_calls == []
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_preflight_size_mismatch_stops_before_upload_and_final_request(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    before_final_calls: list[bool] = []

    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(NonRetryableUploadError, match="expected 6 bytes, found 5") as caught:
            sender.send_video(
                UploadItem(path, "", expected_size=6, expected_sha256="0" * 64),
                random_id=1,
                before_final_request=lambda: before_final_calls.append(True),
            )

    assert caught.value.final_request_started is False
    assert client.upload_calls == []
    assert before_final_calls == []
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in client.requests
    )


def test_upload_item_without_expected_identity_uses_current_file(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    client = FakeClient()

    with make_sender(tmp_path, client=client) as sender:
        assert sender.send_video(upload_item(path, "caption"), random_id=1) == 101
