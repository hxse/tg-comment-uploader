from __future__ import annotations

from pathlib import Path

import pytest

from reupload_fakes import NOW, FakeBot, forwarded, queue_at
from test_reupload_dedup import forwarded_again
from test_reupload_delivery import process_all
from test_reupload_discard import cache_files, configure_cli
from tg_comment_uploader.cli import main


def legacy_queue(tmp_path, *, album=False):
    queue = queue_at(tmp_path)
    originals = [forwarded(1, kind="document", source_post=900, group=7 if album else None)]
    if album:
        originals.append(forwarded(2, kind="photo", source_post=901, group=7))
    for message in originals:
        queue.enqueue(message)
    old = queue.state.jobs[0]
    queue.confirm(old.key, tuple(100 + i for i in range(len(originals))))
    for message in originals:
        queue.enqueue(forwarded_again(message, message.id + 10, group=8 if album else None))
    skipped = queue.state.jobs[1]
    queue.update(skipped.model_copy(update={"status": "duplicate", "duplicate_of": old.key}))
    return queue


@pytest.mark.parametrize("album", [False, True])
def test_legacy_skips_restore_once_with_snapshots_order_and_random_ids_intact(
    tmp_path: Path, album: bool
) -> None:
    queue = legacy_queue(tmp_path, album=album)
    old, skipped = queue.state.jobs
    restored = queue_at(tmp_path)
    assert restored.restore_legacy_skips() == (1, 0)
    assert restored.state.jobs[0] == old
    pending = restored.next_job()
    assert pending is not None
    assert pending.messages == skipped.messages
    assert pending.first_id == skipped.first_id
    assert pending.grouped_id == skipped.grouped_id
    assert pending.duplicate_of is None
    saved = restored.path.read_bytes()
    assert restored.restore_legacy_skips() == (0, 0)
    assert restored.path.read_bytes() == saved
    bot = FakeBot()
    process_all(tmp_path, bot, restored)
    assert len(bot.delivered) == len(skipped.messages)
    assert tuple(bot.delivered) == skipped.random_ids
    assert [n for kind, n in bot.timeline if kind == "download"] == [
        m.message_id for m in skipped.messages
    ]
    finished = queue_at(tmp_path)
    assert finished.restore_legacy_skips() == (0, 0)
    assert finished.next_job() is None


@pytest.mark.parametrize("covers_skip", [False, True])
def test_previous_discard_cutoff_is_respected_when_restoring_old_skips(
    tmp_path: Path, covers_skip: bool
) -> None:
    queue = legacy_queue(tmp_path)
    skipped = queue.state.jobs[1]
    # Earlier versions treated skipped tasks as terminal, leaving this record behind.
    cutoff = int(NOW.timestamp()) + (3600 if covers_skip else 0)
    queue.discard_pending(now=cutoff)
    restored = queue_at(tmp_path)
    result = restored.restore_legacy_skips()
    assert result == ((0, 1) if covers_skip else (1, 0))
    recovered = restored.get(skipped.key)
    assert recovered.status == ("abandoned" if covers_skip else "queued")
    assert recovered.messages == skipped.messages
    assert recovered.duplicate_of is None
    assert queue_at(tmp_path).get(skipped.key) == recovered


def test_status_only_describes_legacy_skips_without_mutating_them(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    queue = legacy_queue(tmp_path)
    before = queue.path.read_bytes()
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    assert main(["reupload", "--status"]) == 0
    assert "legacy skipped tasks will be restored" in capsys.readouterr().out
    assert queue.path.read_bytes() == before
    assert not listeners


@pytest.mark.parametrize("no_resume", [False, True])
def test_cli_restores_skips_before_applying_resume_selection(
    tmp_path: Path, monkeypatch, no_resume: bool
) -> None:
    queue = legacy_queue(tmp_path)
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    assert main(["reupload", *(["--no-resume"] if no_resume else [])]) == 0
    assert len(listeners) == 1
    assert queue.state.jobs[1].status == "queued"
    assert (queue.next_job() is None) is no_resume
    assert queue_at(tmp_path).next_job() is not None


def test_delete_downloads_preserves_files_of_restored_tasks(tmp_path: Path, monkeypatch) -> None:
    queue = legacy_queue(tmp_path)
    completed = cache_files(queue, queue.state.jobs[0].key)
    recoverable = cache_files(queue, queue.state.jobs[1].key)
    configure_cli(tmp_path, monkeypatch, queue)
    assert main(["reupload", "--delete-downloads"]) == 0
    assert not completed.exists()
    assert (recoverable / "media.bin").read_bytes() == b"completed bytes"
    assert queue.state.jobs[1].status == "queued"
