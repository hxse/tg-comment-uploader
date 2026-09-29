"""Lossless snapshots of received messages and supported media metadata."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from telethon import types, utils
from telethon.extensions import BinaryReader

from .errors import AppError
from .telegram_sender import SAFE_UPLOAD_LIMIT_BYTES, UploadItem, UploadThumbnail


def encode_message(message: types.Message) -> str:
    try:
        return base64.b64encode(bytes(message)).decode("ascii")
    except Exception as exc:
        raise AppError("could not persist received Telegram message; no backup was sent") from exc


def decode_message(snapshot: str) -> types.Message:
    try:
        payload = base64.b64decode(snapshot, validate=True)
        with BinaryReader(payload) as reader:
            message = reader.tgread_object()
            if not isinstance(message, types.Message) or reader.read():
                raise ValueError("invalid message snapshot")
        return message
    except Exception as exc:
        raise AppError("invalid stored Telegram message; refusing to send anything") from exc


def accepts_forward(message: Any, chat_id: int, bot_id: int) -> bool:
    if not isinstance(message, types.Message) or message.fwd_from is None:
        return False
    if utils.get_peer_id(message.peer_id) != chat_id:
        return False
    # Channel posts may be marked out for every administrator, so out alone
    # must not exclude a channel forward. Fresh copies have no fwd_from.
    if isinstance(message.peer_id, types.PeerUser) and message.out:
        return False
    return message.from_id != types.PeerUser(bot_id)


def message_kind(message: types.Message) -> Literal["text", "photo", "document"]:
    def unsupported(reason: str) -> AppError:
        return AppError(f"input message {message.id} cannot be backed up unchanged: {reason}")

    # Back up the message body only. Ignore all reply markup, including buttons
    # and reply keyboards; the shared sender does not include it in new messages.
    if message.noforwards or message.ttl_period:
        raise unsupported("protected or disappearing messages are not supported")
    if message.rich_message is not None:
        raise unsupported("rich-message content is not supported")
    media = message.media
    if media is None or isinstance(media, types.MessageMediaEmpty):
        if not message.message:
            raise unsupported("empty content")
        return "text"
    if getattr(media, "ttl_seconds", None):
        raise unsupported("self-destructing media is not supported")
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        if media.live_photo or media.video or media.photo.video_sizes:
            raise unsupported("live photos are not supported")
        if not photo_download_size(media.photo):
            raise unsupported("the full photo is unavailable")
        return "photo"
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        if media.video_cover or media.video_timestamp:
            raise unsupported("custom video covers/timestamps are not supported")
        if any(
            isinstance(a, (types.DocumentAttributeSticker, types.DocumentAttributeCustomEmoji))
            for a in media.document.attributes
        ):
            raise unsupported("stickers and custom-emoji documents are not supported")
        if not 0 < media.document.size <= SAFE_UPLOAD_LIMIT_BYTES:
            raise unsupported(
                f"file size={media.document.size:,} bytes is outside this project's "
                f"allowed range of 1..{SAFE_UPLOAD_LIMIT_BYTES:,} bytes"
            )
        return "document"
    raise unsupported(f"{type(media).__name__} is not supported")


def photo_download_size(photo: types.Photo) -> int:
    sizes: list[int] = []
    for size in photo.sizes:
        if isinstance(size, types.PhotoSize):
            sizes.append(size.size)
        elif isinstance(size, types.PhotoSizeProgressive) and size.sizes:
            sizes.append(max(size.sizes))
        elif isinstance(size, types.PhotoCachedSize):
            sizes.append(len(size.bytes))
    return max(sizes, default=0)


def make_upload_item(
    message: types.Message,
    path: Path,
    size: int,
    digest: str,
    *,
    thumbnail: UploadThumbnail | None = None,
) -> UploadItem:
    kind = message_kind(message)
    if kind == "text":
        raise AppError("text has no upload file")
    document = getattr(message.media, "document", None)
    return UploadItem(
        path,
        message.message or "",
        size,
        digest,
        kind=kind,
        mime_type=document.mime_type if document else None,
        attributes=tuple(document.attributes) if document else None,
        entities=tuple(message.entities or ()),
        thumbnail=thumbnail,
        spoiler=bool(getattr(message.media, "spoiler", False)),
        invert_media=bool(message.invert_media),
        nosound_video=any(
            isinstance(a, types.DocumentAttributeVideo) and a.nosound for a in document.attributes
        )
        if document
        else False,
    )


def _readable_entity(entity: Any) -> dict[str, Any]:
    # Formatted-date entities contain datetime values. Convert only the JSON
    # metadata; snapshots and outgoing Telegram entities retain their TL types.
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in entity.to_dict().items()
    }


def readable_metadata(message: types.Message) -> dict[str, Any]:
    document = getattr(message.media, "document", None)
    return {
        "message_id": message.id,
        "grouped_id": message.grouped_id,
        "text": message.message or "",
        "entities": [_readable_entity(e) for e in message.entities or ()],
        "kind": message_kind(message),
        "mime_type": document.mime_type if document else None,
        # TL snapshot in the queue preserves binary attribute fields too.
        "original_filename": next(
            (
                a.file_name
                for a in document.attributes
                if isinstance(a, types.DocumentAttributeFilename)
            ),
            None,
        )
        if document
        else None,
        "spoiler": bool(getattr(message.media, "spoiler", False)),
        "invert_media": bool(message.invert_media),
    }
