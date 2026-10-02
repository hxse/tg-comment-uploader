"""Resume media bytes through Telethon's offset-aware download iterator."""

from __future__ import annotations

import copy
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from telethon import types

from .errors import AppError
from .local_files import fingerprint_file_async
from .reupload_message import photo_download_size
from .reupload_partial import PartialDownload
from .state_io import fsync_directory
from .telegram_sender import UploadProgress
from .terminal_progress import format_bytes

DOWNLOAD_CHUNK_BYTES = 512 * 1024


async def download_media(
    client: Any,
    message: types.Message,
    path: Path,
    *,
    progress: UploadProgress | None = None,
    verification: UploadProgress | None = None,
    partial_verification: UploadProgress | None = None,
    status: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    source, identity, cached = _source(message)
    total = media_size(source)
    partial = PartialDownload(path, identity=identity, total_size=total)
    completed = await partial.completed_file(path, progress=verification)
    if completed is not None:
        return completed
    with partial:
        await partial.restore(progress=partial_verification)
        if partial.size and status:
            status(
                f"resuming input {message.id} from {format_bytes(partial.size)} / "
                f"{format_bytes(total)}; saved prefix retained"
            )
        if progress:
            progress(partial.size, total)
        if cached is not None:
            if not partial.matches(0, cached[: partial.size]):
                raise AppError("cached photo differs from the partial download")
            partial.write(cached[partial.size :])
            if progress:
                progress(partial.size, total)
        elif partial.size < total:
            # Align requests to full 512 KiB chunks. Check the overlapping
            # prefix of the first response and append only the missing bytes.
            offset = partial.size // DOWNLOAD_CHUNK_BYTES * DOWNLOAD_CHUNK_BYTES
            async with _download_stream(client, source, offset=offset, total=total) as stream:
                async for chunk in stream:
                    if not chunk:
                        break
                    overlap = min(partial.size - offset, len(chunk))
                    if overlap and not partial.matches(offset, chunk[:overlap]):
                        raise AppError("remote media differs from the saved partial download")
                    partial.write(chunk[overlap:])
                    offset += len(chunk)
                    if progress:
                        progress(partial.size, total)
                    if partial.size == total:
                        break
        if partial.size != total:
            raise AppError(f"incomplete download for input {message.id}; partial bytes retained")
    size, digest = await fingerprint_file_async(partial.path, progress=verification)
    if (size, digest) != (partial.size, partial.sha256):
        raise AppError("downloaded file changed before completion; refusing to upload")
    os.replace(partial.path, path)
    fsync_directory(path.parent)
    # Retain the complete checksum until job cleanup to recover a crash before
    # the queue records this completed file. It migrates alongside the media.
    return size, digest


@asynccontextmanager
async def _download_stream(
    client: Any, source: types.Message, *, offset: int, total: int
) -> AsyncIterator[Any]:
    manager = client.iter_download(
        source, offset=offset, request_size=DOWNLOAD_CHUNK_BYTES, file_size=total
    )
    stream = await manager.__aenter__()
    try:
        yield stream
    except BaseException as exc:
        try:
            await manager.__aexit__(type(exc), exc, exc.__traceback__)
        except Exception:
            # Telethon's close() may fail if a media-DC connection failed before
            # initializing the iterator. Keep the original retry/cancellation.
            pass
        raise
    else:
        await manager.__aexit__(None, None, None)


def media_size(message: types.Message) -> int:
    document = getattr(message.media, "document", None)
    if isinstance(document, types.Document):
        return document.size
    photo = getattr(message.media, "photo", None)
    if isinstance(photo, types.Photo):
        return photo_download_size(photo)
    raise AppError("downloaded message has no supported media")


def _source(message: types.Message) -> tuple[types.Message, str, bytes | None]:
    document = getattr(message.media, "document", None)
    if isinstance(document, types.Document):
        return message, f"document:{document.id}:{document.size}", None
    photo = getattr(message.media, "photo", None)
    if not isinstance(photo, types.Photo):
        raise AppError("downloaded message has no supported media")
    sizes = [
        size
        for size in photo.sizes
        if isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive, types.PhotoCachedSize))
    ]
    if not sizes:
        raise AppError("the full photo is unavailable")

    def byte_count(size: Any) -> int:
        if isinstance(size, types.PhotoCachedSize):
            return len(size.bytes)
        if isinstance(size, types.PhotoSizeProgressive):
            return max(size.sizes, default=0)
        return size.size

    selected = max(sizes, key=byte_count)
    # Unlike download_media(), iter_download() chooses the last photo size.
    # Narrow a detached snapshot to the full-size variant, excluding previews.
    source = copy.deepcopy(message)
    assert isinstance(source.media, types.MessageMediaPhoto)
    assert isinstance(source.media.photo, types.Photo)
    source.media.photo.sizes = [selected]
    identity = f"photo:{photo.id}:{selected.type}:{byte_count(selected)}"
    cached = selected.bytes if isinstance(selected, types.PhotoCachedSize) else None
    return source, identity, cached
