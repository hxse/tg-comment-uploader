"""Validate transport inputs and correlate Telegram response IDs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from telethon import types, utils

from .errors import NonRetryableUploadError
from .telegram_sender import UploadItem

MIN_RANDOM_ID = -(2**63)
MAX_RANDOM_ID = 2**63 - 1
NUMERIC_CHAT_ID_PATTERN = re.compile(r"-?[0-9]+")
SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")


class _ResponseValidationError(Exception):
    """A final response cannot safely confirm delivery."""


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
    if item.kind not in ("video", "document", "photo"):
        raise NonRetryableUploadError("upload item media kind is unsupported")
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
    if item.thumbnail is not None:
        from .telegram_sender import UploadThumbnail
        from .thumbnail_image import MAX_THUMBNAIL_BYTES

        thumb = item.thumbnail
        if (
            not isinstance(thumb, UploadThumbnail)
            or item.kind == "photo"
            or not isinstance(thumb.path, Path)
            or thumb.path.suffix.lower() != ".jpg"
            or type(thumb.expected_size) is not int
            or not 0 < thumb.expected_size <= MAX_THUMBNAIL_BYTES
            or not isinstance(thumb.expected_sha256, str)
            or SHA256_HEX_PATTERN.fullmatch(thumb.expected_sha256) is None
        ):
            raise NonRetryableUploadError("upload thumbnail identity or JPEG path is invalid")


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
