"""Move one queue's downloads, retaining durable intent until its new root is safe."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path

from .errors import AppError
from .reupload_queue import ReuploadQueue
from .state_io import ensure_private_directory, fsync_directory


def relocate_downloads(queue: ReuploadQueue, target_root: Path) -> None:
    pending = queue.state.download_migration
    if pending is not None and pending != str(target_root):
        raise AppError(
            f"download migration is unfinished; rerun with --download-dir '{pending}' first"
        )
    old_root = Path(queue.state.download_root)
    if old_root == target_root:
        return
    source = queue.download_directory()
    target = queue.download_directory(target_root)
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise AppError("old and new download directories must not contain each other")
    try:
        _relocate(queue, old_root, target_root, source, target, resuming=pending is not None)
    except OSError as exc:
        raise AppError(
            f"could not move downloads to {target_root}; queue retained, "
            f"retry with the same --download-dir: {exc}"
        ) from exc


def _relocate(
    queue: ReuploadQueue,
    old_root: Path,
    target_root: Path,
    source: Path,
    target: Path,
    *,
    resuming: bool,
) -> None:
    if os.path.lexists(target) and not resuming:
        raise AppError(f"download destination already exists; refusing to overwrite {target}")
    if not os.path.lexists(source):
        # A rename can have completed just before the queue checkpoint was saved.
        if resuming:
            if not os.path.lexists(target):
                raise AppError("download migration has lost both directories; queue retained")
            _check_tree(target)
            fsync_directory(old_root)
            fsync_directory(target_root)
        queue.set_download_location(target_root)
        return
    _check_tree(source)
    if os.path.lexists(target):
        _check_tree(target)
    ensure_private_directory(target_root)
    if not resuming:
        queue.set_download_location(old_root, migration=target_root)
    print(f"moving downloads: {source} -> {target}", flush=True)
    copied = False
    if not os.path.lexists(target):
        try:
            source.rename(target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            copied = True
    else:
        copied = True  # Resume an interrupted cross-filesystem copy.
    if copied:
        shutil.copytree(source, target, dirs_exist_ok=True, copy_function=_copy_file)
        _check_tree(target)
        for directory, _, _ in os.walk(target, topdown=False, onerror=_raise_walk_error):
            fsync_directory(Path(directory))
    fsync_directory(old_root)
    fsync_directory(target_root)
    queue.set_download_location(target_root)
    if copied:
        # The new tree and its queue pointer are durable before removing any old bytes.
        try:
            shutil.rmtree(source)
            fsync_directory(old_root)
        except OSError as exc:
            print(f"downloads moved; old copy could not be fully removed at {source}: {exc}")


def _copy_file(source: str, target: str) -> str:
    result = shutil.copy2(source, target)
    with open(target, "rb") as output:
        os.fsync(output.fileno())
    return result


def _raise_walk_error(error: OSError) -> None:
    raise error


def _check_tree(root: Path) -> None:
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise AppError(f"download migration requires a real directory: {root}")
    for directory, names, files in os.walk(root, onerror=_raise_walk_error, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise AppError(f"download migration refuses symlinks or special files: {path}")
