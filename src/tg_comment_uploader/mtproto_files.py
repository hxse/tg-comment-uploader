"""Shared file integrity checks and bounded MTProto part uploads."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from telethon import functions, types, utils

from .errors import AppError, NonRetryableUploadError, TelegramUploadError
from .mtproto_errors import _translate_exception
from .mtproto_runtime import _drain_cancelled_tasks
from .telegram_sender import UploadItem, UploadProgress, UploadThumbnail, validate_upload_file_size
from .thumbnail_image import MAX_THUMBNAIL_BYTES, validate_thumbnail

if TYPE_CHECKING:
    from .mtproto_sender import MtprotoSender

UPLOAD_PART_SIZE_BYTES = 512 * 1024
UPLOAD_MAX_IN_FLIGHT_PARTS = 8
TELEGRAM_BIG_FILE_THRESHOLD_BYTES = 10 * 1024 * 1024


async def _upload_media(
    self: MtprotoSender,
    client: Any,
    item: UploadItem,
    *,
    progress_callback: UploadProgress | None,
) -> types.InputMediaUploadedDocument | types.InputMediaUploadedPhoto:
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
                if item.kind == "video":
                    attributes, mime_type = utils.get_attributes(
                        file_handle, supports_streaming=self._supports_streaming
                    )
                else:
                    attributes = list(item.attributes or ())
                    mime_type = item.mime_type or "application/octet-stream"
                if item.kind == "video" and not any(
                    isinstance(attribute, types.DocumentAttributeVideo) for attribute in attributes
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

            uploaded = await _upload_open_file(
                self, client, file_handle, item, initial_stat, progress_callback=progress_callback
            )
    except TelegramUploadError:
        raise

    if item.kind == "photo":
        return types.InputMediaUploadedPhoto(file=uploaded, spoiler=item.spoiler)
    thumbnail = await _upload_thumbnail(self, client, item.thumbnail) if item.thumbnail else None
    return types.InputMediaUploadedDocument(
        file=uploaded,
        thumb=thumbnail,
        mime_type=mime_type,
        attributes=attributes,
        nosound_video=item.nosound_video,
        spoiler=item.spoiler,
    )


async def _upload_thumbnail(
    self: MtprotoSender, client: Any, item: UploadThumbnail
) -> types.InputFile:
    try:
        with item.path.open("rb") as source:
            initial = os.fstat(source.fileno())
            validate_thumbnail(source.read(MAX_THUMBNAIL_BYTES + 1))
            source.seek(0)
            uploaded = await _upload_open_file(
                self, client, source, item, initial, progress_callback=None
            )
        if not isinstance(uploaded, types.InputFile):
            raise NonRetryableUploadError("thumbnail is too large")
        return uploaded
    except TelegramUploadError:
        raise
    except (OSError, AppError) as exc:
        raise NonRetryableUploadError(f"could not upload thumbnail {item.path}: {exc}") from exc


async def _upload_open_file(
    self: MtprotoSender,
    client: Any,
    file_handle: BinaryIO,
    item: UploadItem | UploadThumbnail,
    initial_stat: os.stat_result,
    *,
    progress_callback: UploadProgress | None,
) -> types.InputFile | types.InputFileBig:
    file_size = _regular_file_size(file_handle, item.path)
    if file_size != item.expected_size:
        raise NonRetryableUploadError(
            f"upload file size no longer matches its preflight identity: {item.path}"
        )
    callback: UploadProgress | None = None
    if progress_callback is not None:

        def report_progress(sent: int, total: int) -> None:
            progress_callback(int(sent), int(total))

        callback = report_progress
    hashing_reader = _SequentialHashingReader(file_handle)
    try:
        uploaded = await _upload_file_pipelined(
            self,
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
        raise NonRetryableUploadError(f"failed to verify upload file {item.path}: {exc}") from exc
    if (
        position != file_size
        or bytes_read != file_size
        or grew_after_expected_eof
        or final_stat.st_size != initial_stat.st_size
        or final_stat.st_dev != initial_stat.st_dev
        or final_stat.st_ino != initial_stat.st_ino
        or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
    ):
        raise NonRetryableUploadError(f"upload file changed while reading it: {item.path}")
    if not hmac.compare_digest(
        actual_sha256,
        item.expected_sha256,
    ):
        raise NonRetryableUploadError(
            f"upload file content no longer matches its preflight identity: {item.path}"
        )
    return uploaded


async def _upload_file_pipelined(
    self: MtprotoSender,
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
        if progress_callback is not None:
            progress_callback(0, file_size)
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


def _regular_file_size(file_handle: BinaryIO, path: Path) -> int:
    file_stat = os.fstat(file_handle.fileno())
    if not stat.S_ISREG(file_stat.st_mode):
        raise NonRetryableUploadError(f"upload path is not a regular file: {path}")
    if file_stat.st_size <= 0:
        raise NonRetryableUploadError(f"upload file is empty: {path}")
    validate_upload_file_size(path, file_stat.st_size)
    return file_stat.st_size


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
