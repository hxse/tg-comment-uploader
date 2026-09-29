"""Private, atomic JSON checkpoints shared by both workflows."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .errors import AppError
from .strict_json import StrictJsonError, load_strict_json


def ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError("path is not a real directory")
        if os.name == "posix":
            path.chmod(0o700)
    except OSError as exc:
        raise AppError(f"failed to prepare private state directory {path}: {exc}") from exc


def fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_json(path: Path, data: Any, *, label: str) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except OSError as exc:
        raise AppError(f"failed to persist {label}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_private_json(path: Path) -> Any:
    descriptor: int | None = None
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("state path is not a regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("state path changed to a non-regular file")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            descriptor = None
            return load_strict_json(source)
    except (OSError, UnicodeError, StrictJsonError) as exc:
        raise AppError("state is unreadable or corrupt; refusing to send anything") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
