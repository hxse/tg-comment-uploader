from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from filelock import Timeout

from tg_comment_uploader.locking import (
    UPLOAD_LOCK_CONFLICT_MESSAGE,
    UPLOAD_LOCK_RELATIVE_PATH,
    ProjectRootNotFoundError,
    UploadLockSetupError,
    UploadLockUnavailableError,
    find_project_root,
    get_upload_lock_path,
    upload_instance_lock,
)


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "justfile").write_text("default:\n", encoding="utf-8")
    return root


def test_find_project_root_uses_nearest_ancestor_and_returns_absolute_path(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)

    assert find_project_root(nested) == root.resolve()


def test_find_project_root_accepts_a_file_start(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    source = root / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("", encoding="utf-8")

    assert find_project_root(source) == root.resolve()


def test_find_project_root_fails_without_justfile(tmp_path: Path) -> None:
    nested = tmp_path / "not-a-project" / "nested"
    nested.mkdir(parents=True)

    with pytest.raises(ProjectRootNotFoundError, match="ancestor containing justfile"):
        find_project_root(nested)


def test_get_upload_lock_path_is_fixed_and_absolute(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    lock_path = get_upload_lock_path(root)

    assert lock_path == root.resolve() / UPLOAD_LOCK_RELATIVE_PATH
    assert lock_path.is_absolute()


def test_get_upload_lock_path_rejects_an_explicit_non_project_root(tmp_path: Path) -> None:
    with pytest.raises(ProjectRootNotFoundError, match="justfile was not found"):
        get_upload_lock_path(tmp_path)


def test_lock_uses_non_blocking_mode_without_lifetime_and_covers_caller_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_project(tmp_path)
    events: list[str] = []
    constructor: dict[str, Any] = {}

    class RecordingFileLock:
        def __init__(
            self,
            lock_file: Path,
            *,
            blocking: bool,
            lifetime: None,
        ) -> None:
            constructor.update(
                lock_file=lock_file,
                blocking=blocking,
                lifetime=lifetime,
            )
            events.append("created")

        def acquire(self) -> None:
            events.append("acquired")

        def release(self) -> None:
            events.append("released")

    monkeypatch.setattr("tg_comment_uploader.locking.FileLock", RecordingFileLock)

    with upload_instance_lock(root) as lock_path:
        events.append("body")
        assert lock_path == root.resolve() / UPLOAD_LOCK_RELATIVE_PATH
        assert lock_path.parent.is_dir()

    assert constructor == {
        "lock_file": root.resolve() / UPLOAD_LOCK_RELATIVE_PATH,
        "blocking": False,
        "lifetime": None,
    }
    assert events == ["created", "acquired", "body", "released"]


def test_lock_conflict_is_immediate_and_has_a_cli_ready_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_project(tmp_path)
    body_entered = False

    class ContendedFileLock:
        def __init__(self, lock_file: Path, **kwargs: Any) -> None:
            self.lock_file = lock_file

        def acquire(self) -> None:
            raise Timeout(str(self.lock_file))

        def release(self) -> None:
            pytest.fail("an unacquired lock must not be released")

    monkeypatch.setattr("tg_comment_uploader.locking.FileLock", ContendedFileLock)

    with pytest.raises(UploadLockUnavailableError) as exc_info:
        with upload_instance_lock(root):
            body_entered = True

    assert str(exc_info.value) == UPLOAD_LOCK_CONFLICT_MESSAGE
    assert isinstance(exc_info.value.__cause__, Timeout)
    assert body_entered is False


def test_caller_exception_is_preserved_and_lock_is_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_project(tmp_path)
    released = False
    caller_error = Timeout("unrelated.lock")

    class RecordingFileLock:
        def __init__(self, lock_file: Path, **kwargs: Any) -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            nonlocal released
            released = True

    monkeypatch.setattr("tg_comment_uploader.locking.FileLock", RecordingFileLock)

    with pytest.raises(Timeout) as exc_info:
        with upload_instance_lock(root):
            raise caller_error

    assert exc_info.value is caller_error
    assert released is True


def test_existing_unlocked_lock_file_does_not_block_acquisition(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    lock_path = get_upload_lock_path(root)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("stale file contents do not represent ownership", encoding="utf-8")

    with upload_instance_lock(root) as acquired_path:
        assert acquired_path == lock_path


def test_second_lock_instance_fails_while_first_is_held(tmp_path: Path) -> None:
    root = make_project(tmp_path)

    with upload_instance_lock(root):
        with pytest.raises(UploadLockUnavailableError, match="already running"):
            with upload_instance_lock(root):
                pytest.fail("the second lock must not enter its caller body")


def test_lock_setup_error_is_wrapped_for_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_project(tmp_path)

    class BrokenFileLock:
        def __init__(self, lock_file: Path, **kwargs: Any) -> None:
            raise PermissionError("permission denied")

    monkeypatch.setattr("tg_comment_uploader.locking.FileLock", BrokenFileLock)

    with pytest.raises(UploadLockSetupError, match="failed to prepare upload lock") as exc_info:
        with upload_instance_lock(root):
            pytest.fail("a lock that could not be created must not enter its caller body")

    assert isinstance(exc_info.value.__cause__, PermissionError)
