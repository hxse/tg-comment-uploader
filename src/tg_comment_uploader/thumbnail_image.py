"""Small JPEG files, atomic caching, and asynchronous FFmpeg thumbnail preparation."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from .errors import AppError
from .ffmpeg_helpers import terminate_process
from .state_io import ensure_private_directory, fsync_directory

# Conservative dimensions/size used by MTProto clients for document thumbnails.
MAX_THUMBNAIL_BYTES = 20_000
MAX_THUMBNAIL_SIDE = 320


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise AppError("thumbnail is not a complete JPEG")
    offset = 2
    while offset < len(data) - 1:
        if data[offset] != 255:
            break
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xDA, 0xD9):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8 or data[offset + 2] != 8:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width and height:
                return width, height
            break
        offset += length
    raise AppError("thumbnail has no supported JPEG dimensions")


def validate_thumbnail(data: bytes) -> None:
    width, height = jpeg_dimensions(data)
    if len(data) > MAX_THUMBNAIL_BYTES or max(width, height) > MAX_THUMBNAIL_SIDE:
        raise AppError("thumbnail exceeds the 320-pixel / 20-kB upload limits")


def read_thumbnail(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise AppError("thumbnail cache is not a regular file")
        data = source.read(MAX_THUMBNAIL_BYTES + 1)
    validate_thumbnail(data)
    return data


def save_thumbnail(path: Path, data: bytes) -> None:
    validate_thumbnail(data)
    ensure_private_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".part")
    for candidate in (path, temporary):
        if (candidate.exists() or candidate.is_symlink()) and not stat.S_ISREG(
            candidate.lstat().st_mode
        ):
            raise AppError("thumbnail cache is not a regular file")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        if os.name == "posix":
            os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


async def normalize_thumbnail(data: bytes) -> bytes:
    try:
        validate_thumbnail(data)
    except AppError:
        return await _prepare_jpeg(data=data)
    return data


async def video_thumbnail(path: Path, *, duration: float) -> bytes:
    # Skip the usual opening black frame and choose a representative nearby frame.
    return await _prepare_jpeg(video=path, seek=min(max(duration / 10, 0), 3.0))


async def _prepare_jpeg(
    *, data: bytes | None = None, video: Path | None = None, seek: float = 0
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="tg-thumbnail-") as temporary:
        directory = Path(temporary)
        source_path = video
        if source_path is None:
            source_path = directory / "original.jpg"
            source_path.write_bytes(data or b"")
            source_path.chmod(0o600)
        output_path = directory / "thumbnail.jpg"
        for side, quality in ((320, 5), (320, 15), (240, 25), (160, 31)):
            filters = (
                f"scale=w='min({side},iw)':h='min({side},ih)':force_original_aspect_ratio=decrease"
            )
            if video is not None:
                filters = "thumbnail=30," + filters
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
            ]
            if video is not None:
                command += ["-ss", str(seek)]
            command += [
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                filters,
                "-c:v",
                "mjpeg",
                "-pix_fmt",
                "yuvj420p",
                "-q:v",
                str(quality),
                "-f",
                "image2",
                str(output_path),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as exc:
                raise AppError(
                    "FFmpeg is required to prepare this thumbnail; run inside just dev-shell"
                ) from exc
            try:
                deadline = asyncio.get_running_loop().time() + 30
                # Keep intake responsive and use the project's existing process cleanup.
                while process.poll() is None:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AppError("FFmpeg timed out preparing the thumbnail")
                    await asyncio.sleep(0.05)
            finally:
                terminate_process(process)
            if process.returncode or not output_path.is_file():
                raise AppError(
                    "FFmpeg could not prepare a JPEG thumbnail; downloaded video retained"
                )
            output = output_path.read_bytes()
            try:
                validate_thumbnail(output)
            except AppError:
                continue
            return output
    raise AppError("could not prepare a JPEG thumbnail within the upload limits")
