from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tg_comment_uploader.media_compress import CompressionProgress
from tg_comment_uploader.media_split import MediaSplitError, SplitProgress
from tg_comment_uploader.media_workflow import (
    WORK_DIRECTORY_NAME,
    MediaPreparationError,
    prepare_media,
)


def test_under_limit_yields_original_without_workspace(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    with prepare_media(source, "split", hard_limit_bytes=10, target_bytes=9) as prepared:
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

    def fake_split(source: Path, work_dir: Path, **kwargs: object) -> SimpleNamespace:
        assert not (work_dir / "stale.mp4").exists()
        assert kwargs["progress"] is None
        first = work_dir / "part-0001.mp4"
        second = work_dir / "part-0002.mp4"
        first.write_bytes(b"12345")
        second.write_bytes(b"12345")
        return SimpleNamespace(parts=(first, second))

    monkeypatch.setattr("tg_comment_uploader.media_workflow.split_video", fake_split)

    with prepare_media(
        source,
        "split",
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
        source: Path,
        work_dir: Path,
        hard_limit_bytes: int,
        target_bytes: int,
    ) -> Path:
        output = work_dir / "compressed.mp4"
        output.write_bytes(b"small")
        return output

    monkeypatch.setattr("tg_comment_uploader.media_workflow.compress_video", fake_compress)

    with prepare_media(source, "compress", hard_limit_bytes=5, target_bytes=4) as prepared:
        assert [path.name for path in prepared.paths] == ["compressed.mp4"]
        assert prepared.is_media_group is False

    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()


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
        with prepare_media(source, "compress", hard_limit_bytes=5, target_bytes=4):
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
        with prepare_media(source, "split", hard_limit_bytes=5, target_bytes=4):
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
        with prepare_media(source, "split", hard_limit_bytes=5, target_bytes=4):
            pass


def test_error_policy_never_creates_workspace(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"oversized")

    with pytest.raises(MediaPreparationError, match="no upload was attempted"):
        with prepare_media(source, "error", hard_limit_bytes=5, target_bytes=4):
            pass

    assert not (tmp_path / WORK_DIRECTORY_NAME).exists()
