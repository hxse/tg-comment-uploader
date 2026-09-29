"""Stable file fingerprints shared by local uploads and downloaded backups."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable, Generator
from contextlib import closing
from pathlib import Path

from .errors import NonRetryableUploadError
from .telegram_sender import validate_upload_file_size

CHUNK_SIZE = 1024 * 1024


def fingerprint_file(
    path: Path,
    *,
    allow_oversized: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, str]:
    """Hash one stable regular file for pending-operation identity checks."""

    with closing(
        _fingerprint_chunks(path, allow_oversized=allow_oversized, progress=progress)
    ) as steps:
        while True:
            try:
                next(steps)
            except StopIteration as result:
                return result.value


async def fingerprint_file_async(
    path: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, str]:
    """Use the same integrity checks while giving incoming updates time to run."""

    with closing(_fingerprint_chunks(path, progress=progress)) as steps:
        while True:
            try:
                next(steps)
            except StopIteration as result:
                return result.value
            await asyncio.sleep(0)


def _fingerprint_chunks(
    path: Path,
    *,
    allow_oversized: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> Generator[None, None, tuple[int, str]]:

    try:
        with path.open("rb") as input_file:
            metadata = os.fstat(input_file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise NonRetryableUploadError(
                    f"upload path is not a regular file: {path}; not retrying"
                )
            file_size = metadata.st_size
            if file_size == 0:
                raise NonRetryableUploadError(
                    f"upload file is empty: {path}; no upload was attempted"
                )
            if not allow_oversized:
                validate_upload_file_size(path, file_size)

            digest = hashlib.sha256()
            hashed = 0
            if progress is not None:
                progress(0, file_size)
            while hashed < file_size:
                chunk = input_file.read(min(CHUNK_SIZE, file_size - hashed))
                if not chunk:
                    raise NonRetryableUploadError(
                        f"upload file changed while fingerprinting: {path}; "
                        f"expected {file_size:,} bytes, reached EOF after {hashed:,} bytes; "
                        "not retrying"
                    )
                digest.update(chunk)
                hashed += len(chunk)
                if progress is not None:
                    progress(hashed, file_size)
                yield

            if input_file.read(1):
                raise NonRetryableUploadError(
                    f"upload file grew while fingerprinting: {path}; not retrying"
                )

            final_metadata = os.fstat(input_file.fileno())
            if (
                final_metadata.st_size != metadata.st_size
                or final_metadata.st_mtime_ns != metadata.st_mtime_ns
                or final_metadata.st_ino != metadata.st_ino
                or final_metadata.st_dev != metadata.st_dev
            ):
                raise NonRetryableUploadError(
                    f"upload file changed while fingerprinting: {path}; not retrying"
                )
    except NonRetryableUploadError:
        raise
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to fingerprint upload file {path}: {exc}; not retrying"
        ) from exc

    return file_size, digest.hexdigest()
