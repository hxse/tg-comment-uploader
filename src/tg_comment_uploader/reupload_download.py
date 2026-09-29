"""Download and verify every album member before any final send."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telethon import types

from .errors import AppError
from .local_files import fingerprint_file_async
from .reupload_message import (
    decode_message,
    make_upload_item,
    message_kind,
    photo_download_size,
    readable_metadata,
)
from .reupload_queue import DownloadedFile, Job, QueuedMessage, ReuploadQueue
from .reupload_source import REFERENCE_ERRORS, refresh_source, snapshot_source
from .reupload_thumbnail import prepare_thumbnail
from .state_io import ensure_private_directory, fsync_directory, write_private_json
from .telegram_sender import UploadItem, UploadProgress

PreparationProgress = Callable[[str, int, int, int], None]


def job_directory(queue: ReuploadQueue, job: Job) -> Path:
    return queue.download_directory() / job.key


def media_path(queue: ReuploadQueue, job: Job, item: QueuedMessage) -> Path:
    kind = message_kind(decode_message(item.snapshot))
    return (
        job_directory(queue, job)
        / str(item.message_id)
        / ("media.jpg" if kind == "photo" else "media.bin")
    )


async def prepare_job(
    client: Any,
    peer: Any,
    queue: ReuploadQueue,
    job: Job,
    *,
    progress: PreparationProgress | None = None,
    status: Callable[[str], None] | None = None,
) -> list[UploadItem]:
    messages = [decode_message(m.snapshot) for m in job.messages]
    kinds = [message_kind(m) for m in messages]
    if job.grouped_id is not None:
        if "text" in kinds:
            raise AppError("album contains a text-only message")
        if len({bool(m.invert_media) for m in messages}) != 1:
            raise AppError("album members disagree on caption placement")
    uploads: list[UploadItem] = []
    for index, (item, message, kind) in enumerate(zip(job.messages, messages, kinds, strict=True)):
        directory = job_directory(queue, job) / str(item.message_id)
        ensure_private_directory(directory)
        write_private_json(
            directory / "message.json",
            readable_metadata(message),
            label="downloaded message metadata",
        )
        if kind == "text":
            continue
        path = media_path(queue, job, item)

        def report_download(done: int, total: int, *, item_index: int = index) -> None:
            if progress is not None:
                progress("downloading", item_index, done, total)

        def report_verification(done: int, total: int, *, item_index: int = index) -> None:
            if progress is not None:
                progress(
                    "verifying cached download" if item.downloaded else "verifying download",
                    item_index,
                    done,
                    total,
                )

        if item.downloaded is not None and path.exists():
            _require_regular_file(path)
            size, digest = await fingerprint_file_async(path, progress=report_verification)
            if (size, digest) != (item.downloaded.size, item.downloaded.sha256):
                raise AppError(
                    f"downloaded file changed for input {item.message_id}; refusing to send"
                )
        else:
            report_download(0, _media_size(message))
            source = snapshot_source(message, item.download_reference)
            try:
                size, digest = await _download(
                    client,
                    source,
                    path,
                    progress=report_download,
                    verification=report_verification,
                )
            except REFERENCE_ERRORS:
                source = await refresh_source(client, peer, queue, job.key, message)
                report_download(0, _media_size(message))
                try:
                    size, digest = await _download(
                        client,
                        source,
                        path,
                        progress=report_download,
                        verification=report_verification,
                    )
                except REFERENCE_ERRORS as exc:
                    raise AppError(
                        f"input {item.message_id}: Telegram rejected the refreshed media reference; "
                        "the snapshot and completed local downloads are retained"
                    ) from exc
            if item.downloaded and (size, digest) != (item.downloaded.size, item.downloaded.sha256):
                raise AppError(f"re-downloaded input {item.message_id} has different content")
            queue.downloaded(job.key, item.message_id, DownloadedFile(size=size, sha256=digest))

        def report_thumbnail(stage: str, done: int, total: int, *, item_index: int = index) -> None:
            if progress is not None:
                progress(stage, item_index, done, total)

        thumbnail = await prepare_thumbnail(
            client,
            peer,
            queue,
            job.key,
            message,
            path,
            progress=report_thumbnail,
            status=status,
        )
        uploads.append(make_upload_item(message, path, size, digest, thumbnail=thumbnail))
    return uploads


async def _download(
    client: Any,
    message: types.Message,
    path: Path,
    *,
    progress: UploadProgress | None = None,
    verification: UploadProgress | None = None,
) -> tuple[int, str]:
    temporary = path.with_suffix(path.suffix + ".part")
    if temporary.exists() or temporary.is_symlink():
        _require_regular_file(temporary)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        if os.name == "posix":
            os.fchmod(output.fileno(), 0o600)
        result = await client.download_media(message, file=output, progress_callback=progress)
        if result is None:
            raise AppError(f"Telegram did not download media for input {message.id}")
        if progress is not None:
            progress(output.tell(), _media_size(message))
        output.flush()
        os.fsync(output.fileno())
    size, digest = await fingerprint_file_async(temporary, progress=verification)
    if size != _media_size(message):
        raise AppError(f"incomplete download for input {message.id}")
    os.replace(temporary, path)
    fsync_directory(path.parent)
    return size, digest


def _media_size(message: types.Message) -> int:
    document = getattr(message.media, "document", None)
    if isinstance(document, types.Document):
        return document.size
    photo = getattr(message.media, "photo", None)
    if isinstance(photo, types.Photo):
        return photo_download_size(photo)
    raise AppError("downloaded message has no supported media")


def _require_regular_file(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise AppError("download path is not a regular file")


def cleanup_job(queue: ReuploadQueue, job: Job) -> None:
    if not job.terminal:
        raise AppError("cannot clean up downloads for an unfinished reupload")
    directory = job_directory(queue, job)
    for ancestor in (Path(queue.state.download_root), queue.download_directory(), directory):
        if ancestor.is_symlink():
            raise AppError("download directory is a symbolic link")
    for item in job.messages:
        folder = directory / str(item.message_id)
        if folder.is_symlink():
            raise AppError("download directory is a symbolic link")
        for name in (
            "message.json",
            "media.jpg",
            "media.bin",
            "media.jpg.part",
            "media.bin.part",
            "thumbnail.jpg",
            "thumbnail.jpg.part",
        ):
            (folder / name).unlink(missing_ok=True)
        if folder.exists():
            folder.rmdir()
    if directory.exists():
        directory.rmdir()
    if directory.parent.exists():
        fsync_directory(directory.parent)
    if job.cleanup_pending:
        queue.update(job.model_copy(update={"cleanup_pending": False}))
