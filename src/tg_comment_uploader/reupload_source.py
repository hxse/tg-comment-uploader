"""Use the initial media snapshot, refreshing only its expiring download locator."""

from __future__ import annotations

import base64
from typing import Any

from telethon import errors, types

from .errors import AppError
from .reupload_message import decode_message, encode_message
from .reupload_queue import DownloadReference, ReuploadQueue

REFERENCE_ERRORS = (
    errors.FileReferenceEmptyError,
    errors.FileReferenceExpiredError,
    errors.FileReferenceInvalidError,
    errors.FilerefUpgradeNeededError,
    errors.FileIdInvalidError,
    errors.LocationInvalidError,
)


class SourceUnavailableError(AppError):
    """The old media locator cannot be refreshed from the input message."""


def snapshot_source(
    original: types.Message,
    reference: DownloadReference | None,
) -> types.Message:
    # A detached TL snapshot has no input_chat/client: Telethon cannot silently
    # refetch the original post or replace the selected media during download.
    source = decode_message(encode_message(original))
    if reference is not None:
        media = _media_object(source)
        media.access_hash = reference.access_hash
        media.file_reference = base64.b64decode(reference.file_reference, validate=True)
    return source


async def refresh_source(
    client: Any,
    peer: Any,
    queue: ReuploadQueue,
    job_key: str,
    original: types.Message,
) -> types.Message:
    fresh = await client.get_messages(peer, ids=original.id)
    if not isinstance(fresh, types.Message):
        raise SourceUnavailableError(
            f"input {original.id}: Telegram rejected the saved media reference and the input "
            "message is no longer available to refresh it; the incomplete file cannot be "
            "recovered from metadata alone. Completed local downloads are retained"
        )
    old_media, new_media = _media_object(original), _media_object(fresh)
    if (
        type(old_media) is not type(new_media)
        or old_media.id != new_media.id
        or getattr(old_media, "size", None) != getattr(new_media, "size", None)
    ):
        raise SourceUnavailableError(
            f"input {original.id}: its media changed and cannot refresh the initial snapshot; "
            "refusing to download a replacement file"
        )
    if not new_media.file_reference:
        raise SourceUnavailableError(
            f"input {original.id}: Telegram returned an empty media reference"
        )
    reference = DownloadReference(
        access_hash=new_media.access_hash,
        file_reference=base64.b64encode(new_media.file_reference).decode("ascii"),
    )
    # Keep content/formatting/order immutable, but retain a working locator for
    # retries and restarts even if the post is deleted after this refresh.
    queue.cache_download_reference(job_key, original.id, reference)
    return snapshot_source(original, reference)


def _media_object(message: types.Message) -> types.Document | types.Photo:
    document = getattr(message.media, "document", None)
    if isinstance(document, types.Document):
        return document
    photo = getattr(message.media, "photo", None)
    if isinstance(photo, types.Photo):
        return photo
    raise AppError(f"input {message.id}: no matching photo or document is available")
