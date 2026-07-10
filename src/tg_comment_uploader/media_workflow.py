"""Lifecycle management for oversized media preparation."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .media_compress import CompressionError, CompressionProgress, compress_video
from .media_split import MediaSplitError, SplitProgress, split_video

OversizePolicy = Literal["error", "split", "compress"]
ProgressCallback = Callable[[str], None]
SplitProgressCallback = Callable[[SplitProgress], None]
CompressionProgressCallback = Callable[[CompressionProgress], None]
WarningCallback = Callable[[str], None]

WORK_DIRECTORY_NAME = ".tg-comment-uploader-work"


class MediaPreparationError(RuntimeError):
    """A deterministic failure while preparing an oversized source."""


@dataclass(frozen=True)
class PreparedMedia:
    """Upload-ready paths that stay valid for the surrounding context."""

    source: Path
    paths: tuple[Path, ...]
    policy: OversizePolicy

    @property
    def is_media_group(self) -> bool:
        return len(self.paths) > 1


@contextmanager
def prepare_media(
    source: Path,
    policy: OversizePolicy,
    *,
    hard_limit_bytes: int,
    target_bytes: int,
    progress: ProgressCallback | None = None,
    split_progress: SplitProgressCallback | None = None,
    compression_progress: CompressionProgressCallback | None = None,
    warning: WarningCallback | None = None,
) -> Iterator[PreparedMedia]:
    """Yield original or prepared media and clean temporary outputs afterward.

    Prepared files are reused by upload retries inside the context, but never by
    another source or another command invocation.
    """

    source = _validate_source(source)
    source_size = _file_size(source)
    if source_size <= hard_limit_bytes:
        yield PreparedMedia(source=source, paths=(source,), policy=policy)
        return

    if policy == "error":
        raise MediaPreparationError(
            f"upload file is too large: {source}; size={source_size:,} bytes, "
            f"limit={hard_limit_bytes:,} bytes; no upload was attempted"
        )
    if policy not in {"split", "compress"}:
        raise ValueError(f"unsupported oversize policy: {policy}")

    work_dir = source.parent / WORK_DIRECTORY_NAME
    _emit(progress, f"preparing workspace: {work_dir}")

    try:
        _reset_workspace(source, work_dir)
        if policy == "split":
            if split_progress is None:
                _emit(progress, "probing, planning and losslessly splitting oversized video")
            try:
                result = split_video(
                    source,
                    work_dir,
                    hard_limit_bytes=hard_limit_bytes,
                    target_bytes=target_bytes,
                    progress=split_progress,
                )
            except MediaSplitError as exc:
                raise MediaPreparationError(f"failed to split {source}: {exc}") from exc
            paths = result.parts
        else:
            if compression_progress is None:
                _emit(progress, "probing and compressing oversized video with two-pass FFmpeg")
            try:
                compression_callback = compression_progress
                if compression_callback is None and progress is not None:
                    compression_callback = _compression_progress_reporter(progress)
                if compression_callback is None:
                    output = compress_video(
                        source,
                        work_dir,
                        hard_limit_bytes,
                        target_bytes,
                    )
                else:
                    output = compress_video(
                        source,
                        work_dir,
                        hard_limit_bytes,
                        target_bytes,
                        progress=compression_callback,
                    )
            except CompressionError as exc:
                raise MediaPreparationError(f"failed to compress {source}: {exc}") from exc
            paths = (output,)

        _validate_prepared_paths(paths, work_dir, hard_limit_bytes=hard_limit_bytes)
        _emit(progress, f"prepared {len(paths)} upload file(s)")
        yield PreparedMedia(source=source, paths=paths, policy=policy)
    finally:
        _emit(progress, f"cleaning workspace: {work_dir}")
        cleanup_error = _cleanup_workspace(source, work_dir)
        if cleanup_error is not None:
            _warn(
                warning,
                f"failed to clean temporary workspace {work_dir}: {cleanup_error}; "
                "upload results, if already reported, are unchanged",
            )


def _validate_source(source: Path) -> Path:
    if not source.is_absolute():
        raise MediaPreparationError(f"media source path must be absolute: {source}")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise MediaPreparationError(f"failed to resolve media source {source}: {exc}") from exc
    if not resolved.is_file():
        raise MediaPreparationError(f"media source is not a regular file: {resolved}")
    return resolved


def _file_size(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MediaPreparationError(f"failed to inspect media file {path}: {exc}") from exc
    if size <= 0:
        raise MediaPreparationError(f"media file is empty: {path}")
    return size


def _reset_workspace(source: Path, work_dir: Path) -> None:
    _validate_workspace_location(source, work_dir)
    if work_dir.is_symlink():
        raise MediaPreparationError(f"temporary workspace must not be a symlink: {work_dir}")
    if work_dir.exists():
        if not work_dir.is_dir():
            raise MediaPreparationError(f"temporary workspace path is not a directory: {work_dir}")
        try:
            shutil.rmtree(work_dir)
        except OSError as exc:
            raise MediaPreparationError(
                f"failed to clear stale temporary workspace {work_dir}: {exc}"
            ) from exc
    try:
        work_dir.mkdir(mode=0o700, parents=False)
        work_dir.chmod(0o700)
    except OSError as exc:
        raise MediaPreparationError(
            f"failed to create temporary workspace {work_dir}: {exc}"
        ) from exc


def _cleanup_workspace(source: Path, work_dir: Path) -> str | None:
    try:
        _validate_workspace_location(source, work_dir)
        if work_dir.is_symlink():
            return "workspace became a symbolic link; refusing to follow it"
        if not work_dir.exists():
            return None
        if not work_dir.is_dir():
            return "workspace path is no longer a directory"
        shutil.rmtree(work_dir)
    except (OSError, MediaPreparationError) as exc:
        return str(exc)
    return None


def _validate_workspace_location(source: Path, work_dir: Path) -> None:
    expected_parent = source.parent.resolve(strict=True)
    if (
        work_dir.name != WORK_DIRECTORY_NAME
        or work_dir.parent.resolve(strict=True) != expected_parent
    ):
        raise MediaPreparationError(
            f"refuse to use unexpected temporary workspace path: {work_dir}"
        )
    source_resolved = source.resolve(strict=True)
    work_resolved = work_dir.resolve(strict=False)
    if source_resolved == work_resolved or source_resolved.is_relative_to(work_resolved):
        raise MediaPreparationError("source video must not be inside the temporary workspace")


def _validate_prepared_paths(
    paths: tuple[Path, ...],
    work_dir: Path,
    *,
    hard_limit_bytes: int,
) -> None:
    if not paths:
        raise MediaPreparationError("media preparation produced no upload files")
    resolved_work_dir = work_dir.resolve(strict=True)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise MediaPreparationError(f"prepared upload is not a regular file: {path}")
        resolved = path.resolve(strict=True)
        if resolved.parent != resolved_work_dir:
            raise MediaPreparationError(f"prepared upload escaped the workspace: {resolved}")
        size = _file_size(resolved)
        if size > hard_limit_bytes:
            raise MediaPreparationError(
                f"prepared upload is still too large: {resolved}; size={size:,} bytes, "
                f"limit={hard_limit_bytes:,} bytes"
            )
        try:
            resolved.chmod(0o600)
        except OSError as exc:
            raise MediaPreparationError(
                f"failed to secure prepared upload {resolved}: {exc}"
            ) from exc


def _compression_progress_reporter(
    callback: ProgressCallback,
) -> Callable[[CompressionProgress], None]:
    last_key: tuple[object, ...] | None = None

    def report(event: CompressionProgress) -> None:
        nonlocal last_key
        if event.stage != "compressing":
            key: tuple[object, ...] = (event.stage, event.attempt)
            message = event.stage
        else:
            bucket = int((event.fraction or 0.0) * 20)
            key = (event.stage, event.attempt, event.pass_number, bucket)
            percent = min(bucket * 5, 100)
            message = (
                f"compressing attempt {event.attempt}/{event.max_attempts}, "
                f"pass {event.pass_number}/2: {percent}%"
            )
        if key != last_key:
            callback(message)
            last_key = key

    return report


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _warn(callback: WarningCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
