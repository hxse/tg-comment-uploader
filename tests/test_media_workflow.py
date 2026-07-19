from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast

import pytest

from tg_comment_uploader.media_compress import CompressionProgress
from tg_comment_uploader.media_split import MediaSplitError, SplitProgress
from tg_comment_uploader.media_workflow import (
    OUTPUT_DIRECTORY_NAME,
    SNAPSHOT_DIRECTORY_NAME,
    WORK_DIRECTORY_NAME,
    MediaPreparationError,
    prepare_media,
)


class ExpectedIdentity(TypedDict):
    expected_source_size: int
    expected_source_sha256: str


def expected_identity(source: Path) -> ExpectedIdentity:
    content = source.read_bytes()
    return {
        "expected_source_size": len(content),
        "expected_source_sha256": hashlib.sha256(content).hexdigest(),
    }


def test_under_limit_yields_original_without_workspace(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    with prepare_media(
        source,
        "split",
        **expected_identity(source),
        hard_limit_bytes=10,
        target_bytes=9,
    ) as prepared:
        assert prepared.paths == (source.resolve(),)
        assert prepared.is_media_group is False

    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()


def test_split_clears_stale_files_keeps_parts_for_body_and_cleans_afterward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    workspace = tmp_path / WORK_DIRECTORY_NAME
    workspace.mkdir()
    (workspace / "stale.mp4").write_bytes(b"stale")
    messages: list[str] = []

    def fake_split(snapshot: Path, work_dir: Path, **kwargs: object) -> SimpleNamespace:
        assert not (workspace / "stale.mp4").exists()
        assert snapshot == workspace / SNAPSHOT_DIRECTORY_NAME / source.name
        assert snapshot.read_bytes() == b"oversized"
        assert work_dir == workspace / OUTPUT_DIRECTORY_NAME
        assert kwargs["progress"] is None
        if os.name == "posix":
            assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
            assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(work_dir.stat().st_mode) == 0o700
        first = work_dir / "part-0001.mp4"
        second = work_dir / "part-0002.mp4"
        first.write_bytes(b"12345")
        second.write_bytes(b"12345")
        return SimpleNamespace(parts=(first, second))

    monkeypatch.setattr("tg_comment_uploader.media_workflow.split_video", fake_split)

    with prepare_media(
        source,
        "split",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
        progress=messages.append,
    ) as prepared:
        assert prepared.is_media_group is True
        assert all(path.exists() for path in prepared.paths)
        assert workspace.exists()

    assert not workspace.exists()
    assert any("probing, planning and losslessly splitting" in message for message in messages)


def test_compress_uses_one_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")

    def fake_compress(
        snapshot: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        assert snapshot.parent.name == SNAPSHOT_DIRECTORY_NAME
        assert snapshot.read_bytes() == b"oversized"
        assert work_dir.name == OUTPUT_DIRECTORY_NAME
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(
        source,
        "compress",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
    ) as prepared:
        assert [path.name for path in prepared.paths] == ["compressed.mp4"]
        assert prepared.is_media_group is False

    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()


def test_path_replacement_during_snapshot_is_rejected_before_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    original = b"0123456789"
    replacement = b"abcdefghij"
    source.write_bytes(original)
    replacement_path = tmp_path / "replacement.mp4"
    replacement_path.write_bytes(replacement)
    workspace = tmp_path / WORK_DIRECTORY_NAME
    split_calls = 0

    def replace_after_snapshot_open(message: str) -> None:
        if message.startswith("copying verified source snapshot"):
            replacement_path.replace(source)

    def fake_split(snapshot: Path, work_dir: Path, **kwargs: object) -> SimpleNamespace:
        nonlocal split_calls
        del snapshot, work_dir, kwargs
        split_calls += 1
        raise AssertionError("split must not receive a source whose path was replaced")

    monkeypatch.setattr("tg_comment_uploader.media_workflow.split_video", fake_split)

    with pytest.raises(MediaPreparationError, match="changed while creating"):
        with prepare_media(
            source,
            "split",
            expected_source_size=len(original),
            expected_source_sha256=hashlib.sha256(original).hexdigest(),
            hard_limit_bytes=6,
            target_bytes=5,
            progress=replace_after_snapshot_open,
        ):
            pass

    assert split_calls == 0
    assert source.read_bytes() == replacement
    assert not workspace.exists()


def test_compress_snapshot_is_unchanged_when_original_inode_is_modified_and_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    original = b"0123456789"
    replacement = b"abcdefghij"
    source.write_bytes(original)
    original_inode = source.stat().st_ino

    def fake_compress(
        snapshot: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        del hard_limit_bytes, target_bytes
        assert snapshot.read_bytes() == original
        source.write_bytes(replacement)
        assert source.stat().st_ino == original_inode
        output = work_dir / "compressed.mp4"
        output.write_bytes(snapshot.read_bytes()[:5])
        source.write_bytes(original)
        assert source.stat().st_ino == original_inode
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(
        source,
        "compress",
        expected_source_size=len(original),
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        hard_limit_bytes=6,
        target_bytes=5,
    ) as prepared:
        assert prepared.paths[0].read_bytes() == b"01234"

    assert source.read_bytes() == original
    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()


def test_snapshot_hash_rejects_modify_copy_restore_aba_before_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    original = b"0123456789"
    replacement = b"abcdefghij"
    source.write_bytes(original)
    initial_status = source.stat()
    compress_calls = 0

    def mutate_then_restore(message: str) -> None:
        if message.startswith("copying verified source snapshot"):
            source.write_bytes(replacement)
            assert source.stat().st_ino == initial_status.st_ino
        elif message.startswith("verifying stable source snapshot"):
            source.write_bytes(original)
            os.utime(
                source,
                ns=(initial_status.st_atime_ns, initial_status.st_mtime_ns),
            )
            assert source.stat().st_ino == initial_status.st_ino

    def forbidden_compress(*args: object, **kwargs: object) -> Path:
        nonlocal compress_calls
        compress_calls += 1
        raise AssertionError("FFmpeg must not receive a mismatched snapshot")

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", forbidden_compress)

    with pytest.raises(MediaPreparationError, match="changed while creating"):
        with prepare_media(
            source,
            "compress",
            expected_source_size=len(original),
            expected_source_sha256=hashlib.sha256(original).hexdigest(),
            hard_limit_bytes=6,
            target_bytes=5,
            progress=mutate_then_restore,
        ):
            pass

    assert source.read_bytes() == original
    assert compress_calls == 0
    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()


def test_original_can_be_deleted_after_snapshot_and_workspace_still_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    original = b"0123456789"
    source.write_bytes(original)
    workspace = tmp_path / WORK_DIRECTORY_NAME

    def fake_compress(
        snapshot: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        del hard_limit_bytes, target_bytes
        source.unlink()
        output = work_dir / "compressed.mp4"
        output.write_bytes(snapshot.read_bytes()[:5])
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(
        source,
        "compress",
        expected_source_size=len(original),
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        hard_limit_bytes=6,
        target_bytes=5,
    ) as prepared:
        assert prepared.source == source
        assert prepared.paths[0].read_bytes() == b"01234"
        assert not source.exists()

    assert not workspace.exists()


def test_snapshot_identity_failure_cleans_workspace_without_starting_ffmpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"0123456789")
    workspace = tmp_path / WORK_DIRECTORY_NAME
    compress_calls = 0

    def forbidden_compress(*args: object, **kwargs: object) -> Path:
        nonlocal compress_calls
        del args, kwargs
        compress_calls += 1
        raise AssertionError("FFmpeg must not receive an unverified snapshot")

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", forbidden_compress)

    with pytest.raises(MediaPreparationError, match="snapshot identity does not match"):
        with prepare_media(
            source,
            "compress",
            expected_source_size=source.stat().st_size,
            expected_source_sha256="0" * 64,
            hard_limit_bytes=6,
            target_bytes=5,
        ):
            pass

    assert compress_calls == 0
    assert not workspace.exists()


def test_structured_compression_progress_is_forwarded_without_string_adaptation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    stage_messages: list[str] = []
    structured_events: list[CompressionProgress] = []
    expected_event = CompressionProgress(
        stage="compressing",
        attempt=2,
        max_attempts=3,
        pass_number=1,
        encoded_seconds=2.5,
        duration_seconds=10.0,
        fraction=0.25,
    )

    def fake_compress(
        source: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
        *,
        progress: Callable[[CompressionProgress], None],
    ) -> Path:
        progress(expected_event)
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(
        source,
        "compress",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
        progress=stage_messages.append,
        compression_progress=structured_events.append,
    ):
        pass

    assert len(structured_events) == 1
    assert structured_events[0] is expected_event
    assert not any("probing and compressing" in message for message in stage_messages)
    assert any("prepared 1 upload file" in message for message in stage_messages)
    assert any("cleaning workspace" in message for message in stage_messages)
    assert not any("compressing attempt" in message for message in stage_messages)


def test_structured_split_progress_is_forwarded_by_identity_and_fraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    stage_messages: list[str] = []
    structured_events: list[SplitProgress] = []
    expected_event = SplitProgress(
        stage="splitting",
        attempt=2,
        max_attempts=3,
        part_count=2,
        processed_seconds=2.5,
        duration_seconds=10.0,
        fraction=0.25,
    )

    def fake_split(
        source: Path,
        work_dir: Path,
        **kwargs: object,
    ) -> SimpleNamespace:
        callback = cast(Callable[[SplitProgress], None], kwargs["progress"])
        callback(expected_event)
        first = work_dir / "video part-0001.mp4"
        second = work_dir / "video part-0002.mp4"
        first.write_bytes(b"12345")
        second.write_bytes(b"12345")
        return SimpleNamespace(parts=(first, second))

    monkeypatch.setattr("tg_comment_uploader.media_workflow.split_video", fake_split)

    with prepare_media(
        source,
        "split",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
        progress=stage_messages.append,
        split_progress=structured_events.append,
    ):
        pass

    assert len(structured_events) == 1
    assert structured_events[0] is expected_event
    assert structured_events[0].fraction == 0.25
    assert not any("probing, planning" in message for message in stage_messages)
    assert any("preparing workspace" in message for message in stage_messages)
    assert any("prepared 2 upload file" in message for message in stage_messages)
    assert any("cleaning workspace" in message for message in stage_messages)


def test_string_progress_remains_the_compression_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    messages: list[str] = []

    def fake_compress(
        source: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
        *,
        progress: Callable[[CompressionProgress], None],
    ) -> Path:
        progress(
            CompressionProgress(
                stage="compressing",
                attempt=1,
                max_attempts=3,
                pass_number=2,
                fraction=0.25,
            )
        )
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(
        source,
        "compress",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
        progress=messages.append,
    ):
        pass

    assert "compressing attempt 1/3, pass 2/2: 25%" in messages


def test_workspace_is_cleaned_when_upload_body_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    workspace = tmp_path / WORK_DIRECTORY_NAME

    def fake_compress(
        source: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with pytest.raises(RuntimeError, match="upload failed"):
        with prepare_media(
            source,
            "compress",
            **expected_identity(source),
            hard_limit_bytes=5,
            target_bytes=4,
        ):
            raise RuntimeError("upload failed")

    assert not workspace.exists()


def test_cleanup_failure_warns_without_masking_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    warnings: list[str] = []

    def fake_compress(
        source: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)
    monkeypatch.setattr(
        "tg_comment_uploader.media_workflow._cleanup_workspace",
        lambda source, work_dir: "permission denied",
    )

    with prepare_media(
        source,
        "compress",
        **expected_identity(source),
        hard_limit_bytes=5,
        target_bytes=4,
        warning=warnings.append,
    ):
        pass

    assert len(warnings) == 1
    assert "permission denied" in warnings[0]


def test_missing_media_tool_hint_survives_workflow_wrapping_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    workspace = tmp_path / WORK_DIRECTORY_NAME

    def fake_split(source: Path, work_dir: Path, **kwargs: object) -> SimpleNamespace:
        del source, work_dir, kwargs
        raise MediaSplitError(
            'required media tool "ffprobe" was not found; run just dev-shell and retry'
        )

    monkeypatch.setattr("tg_comment_uploader.media_workflow.split_video", fake_split)

    with pytest.raises(MediaPreparationError) as exc_info:
        with prepare_media(
            source,
            "split",
            **expected_identity(source),
            hard_limit_bytes=5,
            target_bytes=4,
        ):
            pass

    message = str(exc_info.value)
    assert "failed to split" in message
    assert "ffprobe" in message
    assert "just dev-shell" in message
    assert not workspace.exists()


def test_preexisting_workspace_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / WORK_DIRECTORY_NAME).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(MediaPreparationError, match="must not be a symlink"):
        with prepare_media(
            source,
            "split",
            **expected_identity(source),
            hard_limit_bytes=5,
            target_bytes=4,
        ):
            pass


def test_error_policy_never_creates_workspace(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")

    with pytest.raises(MediaPreparationError, match="no upload was attempted"):
        with prepare_media(
            source,
            "error",
            **expected_identity(source),
            hard_limit_bytes=5,
            target_bytes=4,
        ):
            pass

    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()
