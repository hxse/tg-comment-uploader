from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path

import pytest

from reupload_fakes import FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import prepare_job
from tg_comment_uploader.reupload_queue import ReuploadQueue
from tg_comment_uploader.reupload_relocate import relocate_downloads


def reopen(queue: ReuploadQueue, root: Path, **kwargs) -> ReuploadQueue:
    return ReuploadQueue(
        queue.path,
        owner=queue.state.owner,
        chat_id=queue.state.chat_id,
        download_root=root,
        **kwargs,
    )


def populated_queue(tmp_path: Path) -> ReuploadQueue:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    namespace = queue.download_directory()
    directory = namespace / "message-1" / "1"
    directory.mkdir(parents=True)
    (directory / "message.json").write_bytes(b'{"text": "keep metadata"}')
    (directory / "media.bin").write_bytes(b"keep completed media")
    (directory / "media.bin.part").write_bytes(b"partial bytes")
    return queue


def tree_bytes(directory: Path) -> dict[Path, bytes]:
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def emulate_cross_device(monkeypatch, source: Path) -> None:
    original = Path.rename

    def rename(self, target):
        if self == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)


@pytest.mark.parametrize("cross_device", [False, True])
def test_moving_legacy_queue_preserves_files_jobs_and_other_chats(
    tmp_path: Path, monkeypatch, cross_device: bool
) -> None:
    queue = populated_queue(tmp_path)
    source = queue.download_directory()
    expected = tree_bytes(source)
    jobs = queue.state.jobs
    raw = json.loads(queue.path.read_text())
    del raw["download_migration"]  # Existing queue-v1 checkpoints remain readable.
    queue.path.write_text(json.dumps(raw))
    other = source.parent / "another-chat"
    other.mkdir()
    (other / "keep.bin").write_bytes(b"unrelated")
    target_root = tmp_path / "系统下载" / "tg-comment-uploader" / "reupload"
    if cross_device:
        emulate_cross_device(monkeypatch, source)
    queue = reopen(queue, target_root, allow_download_root_change=True)
    relocate_downloads(queue, target_root)
    restored = reopen(queue, target_root)
    assert tree_bytes(restored.download_directory()) == expected
    assert restored.state.jobs == jobs
    assert restored.state.download_migration is None
    assert not source.exists()
    assert (other / "keep.bin").read_bytes() == b"unrelated"
    before = restored.path.read_bytes()
    relocate_downloads(restored, target_root)
    assert restored.path.read_bytes() == before


def test_move_reuses_completed_downloads_and_sends_with_original_random_ids(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))
    for message_id in (5, 6):
        queue.enqueue(forwarded(message_id, kind="document", group=7))
    job = queue.next_job()
    assert job is not None
    queue.begin(job)
    bot = FakeBot()
    asyncio.run(prepare_job(bot, bot.input_peer, queue, job))
    queue.sending(job.key)
    before = queue.state
    target_root = tmp_path / "new-downloads"
    relocate_downloads(queue, target_root)
    queue = reopen(queue, target_root)
    assert queue.state.jobs == before.jobs
    assert queue.state.started_through == before.started_through
    bot = FakeBot()
    bot.fail_download = 100  # Only the completed local files are available.
    process_all(tmp_path, bot, queue)
    assert bot.message_lookups == []
    assert not any(kind == "download" for kind, _ in bot.timeline)
    assert bot.sent_payloads == [bot.payload, bot.payload]
    assert tuple(bot.delivered) == job.random_ids
    assert queue.state.jobs[0] == before.jobs[0]
    assert queue.next_job() is None


def test_interruption_after_rename_resumes_before_updating_root(
    tmp_path: Path, monkeypatch
) -> None:
    queue = populated_queue(tmp_path)
    source = queue.download_directory()
    expected = tree_bytes(source)
    target_root = tmp_path / "new-downloads"
    save = queue.set_download_location

    def fail_final_checkpoint(root, *, migration=None):
        if migration is None:
            raise AppError("disk full while saving final checkpoint")
        save(root, migration=migration)

    monkeypatch.setattr(queue, "set_download_location", fail_final_checkpoint)
    with pytest.raises(AppError, match="disk full"):
        relocate_downloads(queue, target_root)
    restored = reopen(queue, target_root, allow_download_root_change=True)
    assert restored.state.download_root == str(source.parent)
    assert restored.state.download_migration == str(target_root)
    assert not source.exists()
    relocate_downloads(restored, target_root)
    assert tree_bytes(restored.download_directory()) == expected
    assert restored.state.download_migration is None


def test_interrupted_cross_device_copy_keeps_source_and_resumes_same_target(
    tmp_path: Path, monkeypatch
) -> None:
    from tg_comment_uploader import reupload_relocate

    queue = populated_queue(tmp_path)
    source = queue.download_directory()
    expected = tree_bytes(source)
    target_root = tmp_path / "new-downloads"
    emulate_cross_device(monkeypatch, source)
    copy = reupload_relocate._copy_file

    def interrupt(source_file, target_file):
        Path(target_file).write_bytes(b"incomplete copy")
        raise KeyboardInterrupt

    monkeypatch.setattr(reupload_relocate, "_copy_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        relocate_downloads(queue, target_root)
    assert tree_bytes(source) == expected
    restored = reopen(queue, target_root, allow_download_root_change=True)
    assert restored.state.download_migration == str(target_root)
    state_bytes = queue.path.read_bytes()
    with pytest.raises(AppError, match="migration is unfinished"):
        relocate_downloads(restored, tmp_path / "different-target")
    assert queue.path.read_bytes() == state_bytes
    monkeypatch.setattr(reupload_relocate, "_copy_file", copy)
    relocate_downloads(restored, target_root)
    assert tree_bytes(restored.download_directory()) == expected
    assert not source.exists()


def test_failure_saving_move_intent_leaves_all_downloads_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    queue = populated_queue(tmp_path)
    source = queue.download_directory()
    expected = tree_bytes(source)
    checkpoint = queue.path.read_bytes()

    def fail(*args, **kwargs):
        raise AppError("cannot save queue")

    monkeypatch.setattr(queue, "set_download_location", fail)
    target_root = tmp_path / "new-downloads"
    with pytest.raises(AppError, match="cannot save queue"):
        relocate_downloads(queue, target_root)
    assert tree_bytes(source) == expected
    assert queue.path.read_bytes() == checkpoint
    assert not queue.download_directory(target_root).exists()


@pytest.mark.parametrize("conflict", ["directory", "nested", "symlink"])
def test_conflicting_or_unsafe_destination_is_not_overwritten(
    tmp_path: Path, conflict: str
) -> None:
    queue = populated_queue(tmp_path)
    source = queue.download_directory()
    expected = tree_bytes(source)
    target_root = tmp_path / "new-downloads"
    if conflict == "nested":
        target_root = source / "nested"
    else:
        target_root.mkdir()
        target = queue.download_directory(target_root)
        if conflict == "symlink":
            target.symlink_to(source, target_is_directory=True)
        else:
            target.mkdir()
            (target / "precious.bin").write_bytes(b"do not overwrite")
    saved = queue.path.read_bytes()
    with pytest.raises(AppError, match="already exists|must not contain"):
        relocate_downloads(queue, target_root)
    assert tree_bytes(source) == expected
    assert queue.path.read_bytes() == saved
    if conflict == "directory":
        assert (
            queue.download_directory(target_root) / "precious.bin"
        ).read_bytes() == b"do not overwrite"


def test_move_refuses_source_symlinks(tmp_path: Path) -> None:
    queue = populated_queue(tmp_path)
    (queue.download_directory() / "external").symlink_to(tmp_path, target_is_directory=True)
    saved = queue.path.read_bytes()
    with pytest.raises(AppError, match="refuses symlinks"):
        relocate_downloads(queue, tmp_path / "new-downloads")
    assert queue.path.read_bytes() == saved


def test_empty_download_root_can_change_without_creating_media_folders(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    target_root = tmp_path / "new-downloads"
    relocate_downloads(queue, target_root)
    assert reopen(queue, target_root).state.jobs == queue.state.jobs
    assert not target_root.exists()


def test_directory_override_cannot_bypass_chat_or_bot_identity(tmp_path: Path) -> None:
    queue = populated_queue(tmp_path)
    with pytest.raises(AppError, match="different bot, chat"):
        ReuploadQueue(
            queue.path,
            owner="a different bot",
            chat_id=queue.state.chat_id,
            download_root=tmp_path / "new-downloads",
            allow_download_root_change=True,
        )
