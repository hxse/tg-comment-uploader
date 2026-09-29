"""Project-wide locking for upload commands.

The lock is deliberately independent from input files and profiles: every upload
started from this project resolves to the same lock file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

UPLOAD_LOCK_RELATIVE_PATH = Path(".local/tg-comment-uploader/upload.lock")
UPLOAD_LOCK_CONFLICT_MESSAGE = (
    "another tg-comment-uploader upload or reupload command is already running"
)


class UploadLockError(RuntimeError):
    """Base class for errors while locating or acquiring the upload lock."""


class ProjectRootNotFoundError(UploadLockError):
    """Raised when the project root containing ``justfile`` cannot be found."""


class UploadLockSetupError(UploadLockError):
    """Raised when the lock directory or operating-system lock cannot be prepared."""


class UploadLockUnavailableError(UploadLockError):
    """Raised when another upload command currently owns the project lock."""


def find_project_root(start: Path | None = None) -> Path:
    """Return the nearest ancestor containing ``justfile``.

    By default the search starts at this module, rather than the current working
    directory, so changing directories cannot make two invocations from the same
    checkout resolve to different lock files. ``start`` exists for callers and
    tests that need to identify another checkout explicitly.
    """

    candidate = (start if start is not None else Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        if (directory / "justfile").is_file():
            return directory

    raise ProjectRootNotFoundError(
        f"could not find tg-comment-uploader project root from {candidate}; "
        "expected an ancestor containing justfile"
    )


def get_upload_lock_path(project_root: Path | None = None) -> Path:
    """Return the absolute, project-wide upload lock path."""

    root = find_project_root() if project_root is None else project_root.resolve()
    if not (root / "justfile").is_file():
        raise ProjectRootNotFoundError(
            f"invalid tg-comment-uploader project root {root}; justfile was not found"
        )
    return root / UPLOAD_LOCK_RELATIVE_PATH


@contextmanager
def upload_instance_lock(project_root: Path | None = None) -> Iterator[Path]:
    """Acquire the non-blocking project upload lock for the caller-controlled body.

    The yielded path is useful for diagnostics. The ``FileLock`` object remains
    strongly referenced for the entire body and has no lifetime/TTL, which is
    required because video preparation and upload can take hours.
    """

    lock_path = get_upload_lock_path(project_root)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(lock_path, blocking=False, lifetime=None)
    except OSError as exc:
        raise UploadLockSetupError(f"failed to prepare upload lock at {lock_path}: {exc}") from exc

    try:
        lock.acquire()
    except Timeout as exc:
        raise UploadLockUnavailableError(UPLOAD_LOCK_CONFLICT_MESSAGE) from exc
    except OSError as exc:
        raise UploadLockSetupError(f"failed to acquire upload lock at {lock_path}: {exc}") from exc

    try:
        yield lock_path
    finally:
        lock.release()
