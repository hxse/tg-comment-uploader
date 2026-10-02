"""Offline MTProto fixture: received files and idempotent final requests."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telethon import functions, types, utils

from tg_comment_uploader.mtproto_sender import MtprotoSender
from tg_comment_uploader.reupload_queue import ReuploadQueue
from tg_comment_uploader.telegram_sender import FinalRequestStatus

CHAT = -1000000000123
BOT = 123456
PEER = types.PeerChannel(123)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
THUMBNAIL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMQD/2wBDAAgEBAQEBAUFBQUF"
    "BQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/"
    "xABMAAEBAAAAAAAAAAAAAAAAAAAABgEBAQAAAAAAAAAAAAAAAAAABgcQAQAAAAAAAAAAAAAAAAAA"
    "AAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAQABADASIAAhEAAxEA/9oADAMBAAIRAxEAPwCLAE1/f//Z"
)


def forwarded(
    message_id: int,
    *,
    kind: str = "text",
    group: int | None = None,
    text: str = "原样 😀 text",
    payload: bytes = b"downloaded media bytes",
    source_post: int | None = None,
) -> types.Message:
    media = None
    if kind == "photo":
        media = types.MessageMediaPhoto(photo=photo(message_id))
    if kind == "document":
        media = types.MessageMediaDocument(
            document=types.Document(
                id=message_id,
                access_hash=345,
                file_reference=b"source-ref",
                date=NOW,
                mime_type="video/mp4",
                size=len(payload),
                dc_id=2,
                thumbs=[types.PhotoCachedSize("m", 16, 16, THUMBNAIL_JPEG)],
                attributes=[
                    types.DocumentAttributeFilename("原名.mp4"),
                    types.DocumentAttributeVideo(4.5, 1280, 720, supports_streaming=True),
                ],
            )
        )
    return types.Message(
        id=message_id,
        peer_id=PEER,
        date=NOW,
        message=text,
        media=media,
        grouped_id=group,
        fwd_from=types.MessageFwdHeader(
            date=NOW, from_id=types.PeerChannel(999), channel_post=source_post
        ),
        entities=[types.MessageEntityBold(0, 2)],
        post=True,
    )


def photo(photo_id: int) -> types.Photo:
    return types.Photo(
        id=photo_id,
        access_hash=456,
        file_reference=b"ref",
        date=NOW,
        dc_id=2,
        sizes=[types.PhotoSize("x", 800, 600, 22)],
    )


def queue_at(tmp_path: Path) -> ReuploadQueue:
    return ReuploadQueue(
        tmp_path / "state" / "queue.json",
        owner="test-bot-config",
        chat_id=CHAT,
        download_root=tmp_path / "downloads",
    )


def sender_at(
    tmp_path: Path,
    bot: Any,
    *,
    final_request_status: FinalRequestStatus | None = None,
) -> MtprotoSender:
    return MtprotoSender(
        api_id=123,
        api_hash="fake-api-hash",
        bot_token=f"{BOT}:fake-token",
        session_path=tmp_path / "session" / "bot.session",
        chat_id=utils.get_peer_id(bot.peer),
        reply_message_id=None,
        supports_streaming=True,
        client_factory=lambda *args, **kwargs: bot,
        final_request_status=final_request_status,
    )


class FakeBot:
    def __init__(self, messages: list[types.Message] | None = None) -> None:
        self.peer: Any = PEER
        self.input_peer: Any = types.InputPeerChannel(123, 456)
        self.sources = {m.id: m for m in messages or []}
        self.payload = b"downloaded media bytes"
        self.parts: dict[int, dict[int, bytes]] = {}
        self.materialized: dict[int, bytes] = {}
        self.requests: list[Any] = []
        self.message_lookups: list[int] = []
        self.timeline: list[tuple[str, Any]] = []
        self.delivered: dict[int, int] = {}
        self.sent_payloads: list[bytes] = []
        self.sent_text: list[str] = []
        self.fail_final = 0
        self.fail_download = 0
        self.download_hook: Any = None
        self.download_chunk_hook: Any = None
        self.download_offsets: list[tuple[int, int]] = []
        self.download_streams_closed = 0
        self.thumbnail_payload = THUMBNAIL_JPEG
        self.thumbnail_downloads: list[int] = []
        self.thumbnail_hook: Any = None
        self.send_hook: Any = None
        self.handler: Any = None
        self.connected = True
        self.session = SimpleNamespace(filename=None)
        self._disconnected: asyncio.Future[None] | None = None

    @property
    def disconnected(self) -> asyncio.Future[None]:
        if self._disconnected is None:
            self._disconnected = asyncio.get_running_loop().create_future()
        return self._disconnected

    async def start(self, *, bot_token: str) -> None:
        assert bot_token == f"{BOT}:fake-token"

    async def get_me(self) -> Any:
        return SimpleNamespace(id=BOT, bot=True)

    async def get_input_entity(self, chat_id: int) -> Any:
        assert chat_id == utils.get_peer_id(self.peer)
        return self.input_peer

    async def get_messages(self, peer: Any, *, ids: int) -> types.Message | None:
        self.message_lookups.append(ids)
        return self.sources.get(ids)

    async def download_media(
        self, message: types.Message, *, file: Any, progress_callback=None, thumb=None
    ) -> Any:
        if thumb is not None:
            self.thumbnail_downloads.append(message.id)
            if self.thumbnail_hook:
                await self.thumbnail_hook(message)
            file.write(self.thumbnail_payload)
            if progress_callback is not None:
                progress_callback(len(self.thumbnail_payload), len(self.thumbnail_payload))
            return file
        self.timeline.append(("download", message.id))
        file.write(self.payload[:4])
        if progress_callback is not None:
            progress_callback(4, len(self.payload))
        if self.download_hook:
            await self.download_hook(message)
        if self.fail_download:
            self.fail_download -= 1
            raise ConnectionError("simulated download interruption")
        file.write(self.payload[4:])
        if progress_callback is not None:
            progress_callback(len(self.payload), len(self.payload))
        return file

    async def catch_up(self) -> None:
        pass

    @asynccontextmanager
    async def iter_download(
        self, message: types.Message, *, offset=0, request_size=512 * 1024, file_size=None
    ):
        async def chunks():
            self.timeline.append(("download", message.id))
            self.download_offsets.append((message.id, offset))
            start = offset
            first = self.payload[start : start + 4]
            if first:
                yield first
                start += len(first)
                if self.download_chunk_hook:
                    await self.download_chunk_hook(message, start)
            if self.download_hook:
                await self.download_hook(message)
            if self.fail_download:
                self.fail_download -= 1
                raise ConnectionError("simulated download interruption")
            while start < len(self.payload):
                chunk = self.payload[start : start + request_size]
                yield chunk
                start += len(chunk)
                if self.download_chunk_hook:
                    await self.download_chunk_hook(message, start)

        stream = chunks()
        try:
            yield stream
        finally:
            await stream.aclose()
            self.download_streams_closed += 1

    async def disconnect(self) -> None:
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def is_connected(self) -> bool:
        return self.connected

    def file_bytes(self, handle: Any) -> bytes:
        parts = self.parts[handle.id]
        return b"".join(parts[i] for i in sorted(parts))

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if isinstance(
            request, (functions.upload.SaveFilePartRequest, functions.upload.SaveBigFilePartRequest)
        ):
            self.parts.setdefault(request.file_id, {})[request.file_part] = request.bytes
            return True
        if isinstance(request, functions.messages.UploadMediaRequest):
            new_id = 30000 + len(self.materialized)
            self.materialized[new_id] = self.file_bytes(request.media.file)
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                return types.MessageMediaPhoto(photo=photo(new_id))
            return types.MessageMediaDocument(
                document=types.Document(
                    new_id,
                    456,
                    b"new-ref",
                    NOW,
                    request.media.mime_type,
                    len(self.materialized[new_id]),
                    2,
                    request.media.attributes,
                )
            )
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            ids = [m.random_id for m in request.multi_media]
            self.sent_payloads.extend(self.materialized[m.media.id.id] for m in request.multi_media)
            texts = [m.message for m in request.multi_media]
        elif isinstance(
            request, (functions.messages.SendMediaRequest, functions.messages.SendMessageRequest)
        ):
            ids, texts = [request.random_id], [request.message]
            if isinstance(request, functions.messages.SendMediaRequest):
                self.sent_payloads.append(self.file_bytes(request.media.file))
        else:
            raise AssertionError(f"unexpected API method {type(request).__name__}")
        if self.send_hook:
            self.send_hook(request)
        self.timeline.append(("send", ids))
        self.sent_text.extend(texts)
        updates: list[Any] = []
        for random_id, text in zip(ids, texts, strict=True):
            message_id = self.delivered.setdefault(random_id, 10000 + len(self.delivered))
            message = types.Message(
                message_id,
                self.peer,
                NOW,
                text,
                grouped_id=777 if len(ids) > 1 else None,
                out=True,
                post=True,
            )
            update_type = (
                types.UpdateNewMessage
                if isinstance(self.peer, types.PeerUser)
                else types.UpdateNewChannelMessage
            )
            updates.extend(
                [
                    types.UpdateMessageID(message_id, random_id),
                    update_type(message, 1, 1),
                ]
            )
            if self.handler:
                await self.handler(SimpleNamespace(message=message))
        if self.fail_final:
            self.fail_final -= 1
            raise TimeoutError("simulated lost final response")
        return types.Updates(updates, [], [], NOW, 1)
