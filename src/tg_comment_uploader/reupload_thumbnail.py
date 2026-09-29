"""Preserve source thumbnails, falling back to a frame from verified local video."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from telethon import types, utils

from .errors import AppError
from .reupload_queue import ReuploadQueue
from .reupload_records import ThumbnailFile
from .reupload_source import (
    REFERENCE_ERRORS,
    SourceUnavailableError,
    refresh_source,
    snapshot_source,
)
from .telegram_sender import UploadThumbnail
from .thumbnail_image import normalize_thumbnail, read_thumbnail, save_thumbnail, video_thumbnail

ThumbnailProgress = Callable[[str, int, int], None]
MAX_SOURCE_THUMBNAIL_BYTES = 4 * 1024 * 1024


class _ThumbnailBuffer(io.BytesIO):
    def write(self, data: Any) -> int:
        if self.tell() + len(data) > MAX_SOURCE_THUMBNAIL_BYTES:
            raise AppError("source thumbnail is unexpectedly large")
        return super().write(data)


def _select_thumbnail(document: types.Document) -> Any:
    sizes = [
        t
        for t in document.thumbs or ()
        if isinstance(
            t,
            (
                types.PhotoSize,
                types.PhotoCachedSize,
                types.PhotoSizeProgressive,
                types.PhotoStrippedSize,
            ),
        )
    ]
    return max(sizes, key=lambda t: getattr(t, "w", 0) * getattr(t, "h", 0), default=None)


async def _download_thumbnail(
    client: Any, message: types.Message, thumb: Any, report: Any
) -> bytes:
    if isinstance(thumb, types.PhotoCachedSize):
        return thumb.bytes
    if isinstance(thumb, types.PhotoStrippedSize):
        return utils.stripped_photo_to_jpg(thumb.bytes)
    size = (
        max(thumb.sizes, default=0) if isinstance(thumb, types.PhotoSizeProgressive) else thumb.size
    )
    if not 0 < size <= MAX_SOURCE_THUMBNAIL_BYTES:
        raise AppError("source thumbnail size is invalid")
    # Telethon's document downloader requires a size field, including for progressive JPEGs.
    selector = types.PhotoSize(thumb.type, thumb.w, thumb.h, size)
    if report:
        report(0, size)
    with _ThumbnailBuffer() as output:
        result = await client.download_media(
            message, file=output, thumb=selector, progress_callback=report
        )
        data = output.getvalue()
    if result is None or len(data) != size:
        raise AppError("source thumbnail download is incomplete; refusing to upload")
    return data


async def prepare_thumbnail(
    client: Any,
    peer: Any,
    queue: ReuploadQueue,
    job_key: str,
    message: types.Message,
    video_path: Path,
    *,
    progress: ThumbnailProgress | None = None,
    status: Callable[[str], None] | None = None,
) -> UploadThumbnail | None:
    document = getattr(message.media, "document", None)
    if not isinstance(document, types.Document):
        return None
    item = next(m for m in queue.get(job_key).messages if m.message_id == message.id)
    path = video_path.parent / "thumbnail.jpg"
    previous = item.thumbnail
    if previous is not None and (path.exists() or path.is_symlink()):
        data = read_thumbnail(path)
        if (len(data), hashlib.sha256(data).hexdigest()) != (previous.size, previous.sha256):
            raise AppError(f"cached thumbnail changed for input {message.id}; refusing to send")
        if progress:
            progress("verifying cached thumbnail", len(data), len(data))
        return UploadThumbnail(path, previous.size, previous.sha256)

    thumb = _select_thumbnail(document)
    if previous is not None and previous.source == "generated":
        thumb = None
    data: bytes | None = None
    source_kind: Literal["original", "generated"] = "original"

    def report(done: int, total: int) -> None:
        if progress:
            progress("downloading thumbnail", done, total)

    if thumb is not None:
        source = snapshot_source(message, item.download_reference)
        try:
            data = await _download_thumbnail(client, source, thumb, report)
        except REFERENCE_ERRORS:
            try:
                source = await refresh_source(client, peer, queue, job_key, message)
                data = await _download_thumbnail(client, source, thumb, report)
            except (SourceUnavailableError, *REFERENCE_ERRORS):
                if status:
                    status(f"input {message.id}: original thumbnail unavailable; using local video")
    if data is not None:
        if len(data) > MAX_SOURCE_THUMBNAIL_BYTES:
            raise AppError("source thumbnail is unexpectedly large")
        data = await normalize_thumbnail(data)
    else:
        video = next(
            (a for a in document.attributes if isinstance(a, types.DocumentAttributeVideo)), None
        )
        if video is None:
            if thumb is not None:
                raise AppError(f"original thumbnail unavailable for input {message.id}")
            return None
        if status:
            status(f"input {message.id}: generating thumbnail from downloaded video")
        data = await video_thumbnail(video_path, duration=video.duration)
        source_kind = "generated"

    record = ThumbnailFile(
        size=len(data), sha256=hashlib.sha256(data).hexdigest(), source=source_kind
    )
    if previous is not None and record != previous:
        raise AppError(f"recreated thumbnail differs for input {message.id}; refusing to send")
    save_thumbnail(path, data)
    queue.thumbnail_downloaded(job_key, message.id, record)
    if progress:
        progress("verifying thumbnail", record.size, record.size)
    return UploadThumbnail(path, record.size, record.sha256)
