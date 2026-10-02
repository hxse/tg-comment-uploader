"""Durable, source-bound checksums for a partially downloaded media file."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import Field, ValidationError

from .errors import AppError
from .local_files import fingerprint_file_async
from .reupload_records import Record
from .state_io import read_private_json, write_private_json
from .telegram_sender import UploadProgress

CHECKPOINT_BYTES = 8 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class PartialCheckpoint(Record):
    version: Literal[1] = 1
    identity: str
    total_size: int = Field(gt=0)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PartialDownload:
    def __init__(self, path: Path, *, identity: str, total_size: int) -> None:
        self.path = path.with_suffix(path.suffix + ".part")
        self.checkpoint_path = self.path.with_suffix(self.path.suffix + ".json")
        self.identity, self.total_size = identity, total_size
        self.size = 0
        self._digest = hashlib.sha256()
        self._saved_size = -1
        self._ready = False
        self._file: BinaryIO | None = None

    def read_checkpoint(self) -> PartialCheckpoint | None:
        if not os.path.lexists(self.checkpoint_path):
            return None
        try:
            saved = PartialCheckpoint.model_validate(read_private_json(self.checkpoint_path))
        except ValidationError as exc:
            raise AppError("invalid partial download checkpoint; refusing to resume") from exc
        if (
            saved.identity != self.identity
            or saved.total_size != self.total_size
            or saved.size > self.total_size
        ):
            raise AppError("partial download belongs to different media; refusing to resume")
        return saved

    async def completed_file(
        self, path: Path, *, progress: UploadProgress | None
    ) -> tuple[int, str] | None:
        # Recover a crash between the final rename and the queue checkpoint.
        if not os.path.lexists(path) or os.path.lexists(self.path):
            return None
        saved = self.read_checkpoint()
        if saved is None or saved.size != self.total_size:
            return None
        _require_regular(path)
        result = await fingerprint_file_async(path, progress=progress)
        if result != (saved.size, saved.sha256):
            raise AppError("completed download changed before its queue checkpoint")
        return result

    def __enter__(self) -> PartialDownload:
        if os.path.lexists(self.path):
            _require_regular(self.path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        self._file = os.fdopen(descriptor, "r+b")
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AppError("partial download is not a regular file")
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
        except BaseException:
            self._file.close()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        try:
            if self._ready:
                # Also runs on network failure, cancellation and Ctrl+C.
                self.checkpoint()
        finally:
            self.file.close()

    @property
    def file(self) -> BinaryIO:
        assert self._file is not None
        return self._file

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    async def restore(self, *, progress: UploadProgress | None = None) -> None:
        saved = self.read_checkpoint()
        initial = os.fstat(self.file.fileno())
        if initial.st_size > self.total_size:
            raise AppError("partial download exceeds the source file size")
        # A missing/empty file can be downloaded again. Otherwise keep only
        # bytes covered by the durable checksum, discarding a crash's loose tail.
        size = saved.size if saved and initial.st_size else initial.st_size
        if initial.st_size < size:
            raise AppError("partial download was truncated; refusing to resume")
        if progress and size:
            progress(0, size)
        hashed = 0
        while hashed < size:
            chunk = self.file.read(min(HASH_CHUNK_BYTES, size - hashed))
            if not chunk:
                raise AppError("partial download changed while verifying")
            self._digest.update(chunk)
            hashed += len(chunk)
            if progress:
                progress(hashed, size)
            await asyncio.sleep(0)
        final = os.fstat(self.file.fileno())
        if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
            raise AppError("partial download changed while verifying")
        if saved and size == saved.size and self.sha256 != saved.sha256:
            raise AppError("partial download checksum mismatch; refusing to resume")
        self.size = size
        self.file.truncate(size)
        self.file.seek(size)
        self._ready = True
        # Old versions left .part files without a checksum. Bind that prefix to
        # this snapshot once; all later restarts verify it against this record.
        self.checkpoint()

    def matches(self, offset: int, data: bytes | memoryview) -> bool:
        self.file.seek(offset)
        actual = self.file.read(len(data))
        self.file.seek(self.size)
        return actual == data

    def write(self, data: bytes | memoryview) -> None:
        if self.size + len(data) > self.total_size:
            raise AppError("download is larger than its initial snapshot; refusing to upload")
        if self.file.write(data) != len(data):
            raise AppError("could not write complete download chunk")
        self._digest.update(data)
        self.size += len(data)
        if self.size - self._saved_size >= CHECKPOINT_BYTES:
            self.checkpoint()

    def checkpoint(self) -> None:
        if self.size == self._saved_size:
            return
        self.file.flush()
        os.fsync(self.file.fileno())
        saved = PartialCheckpoint(
            identity=self.identity,
            total_size=self.total_size,
            size=self.size,
            sha256=self.sha256,
        )
        write_private_json(
            self.checkpoint_path, saved.model_dump(mode="json"), label="partial download checkpoint"
        )
        self._saved_size = self.size


def _require_regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise AppError("download path is not a regular file")
