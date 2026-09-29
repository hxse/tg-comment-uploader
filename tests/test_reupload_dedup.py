from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
from telethon import types

from reupload_fakes import FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError


def forwarded_again(message, message_id, *, group=None):
    message = copy.deepcopy(message)
    message.id = message_id
    message.grouped_id = group
    message.date += timedelta(hours=1)
    media = getattr(message.media, "document", None) or getattr(message.media, "photo", None)
    if media:
        media.file_reference = b"refreshed reference"
        media.access_hash += 1
    return message


@pytest.mark.parametrize("kind", ["text", "photo", "document"])
def test_new_forward_downloads_again_but_replayed_input_does_not_repeat(
    tmp_path: Path, kind: str, capsys
) -> None:
    first = forwarded(1, kind=kind, source_post=900)
    second = forwarded_again(first, 2)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.enqueue(second)
    assert not queue.enqueue(first)
    assert not queue.enqueue(second)
    bot = FakeBot([first, second])
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 2
    assert [j.status for j in queue.state.jobs] == ["confirmed", "confirmed"]
    assert [n for kind, n in bot.timeline if kind == "download"] == (
        [] if kind == "text" else [1, 2]
    )
    assert "skipped duplicate" not in capsys.readouterr().out
    restored = queue_at(tmp_path)
    assert not restored.enqueue(second)
    assert restored.next_job() is None
    restored.enqueue(forwarded_again(first, 3))
    resumed = FakeBot()
    resumed.delivered = dict(bot.delivered)
    process_all(tmp_path, resumed, restored)
    assert len(resumed.delivered) == 3
    assert restored.state.jobs[-1].status == "confirmed"
    assert [n for kind, n in resumed.timeline if kind == "download"] == (
        [] if kind == "text" else [3]
    )


def test_repeated_four_video_album_is_a_separate_complete_backup(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    first = [forwarded(i, kind="document", group=7, source_post=1000 + i) for i in (5, 6, 7, 8)]
    second = [forwarded_again(m, m.id + 6, group=8) for m in first]
    for message in first + second:
        queue.enqueue(message)
    original_ids = [j.random_ids for j in queue.state.jobs]
    bot = FakeBot(first + second)
    process_all(tmp_path, bot, queue)
    assert [n for kind, n in bot.timeline if kind == "download"] == [5, 6, 7, 8, 11, 12, 13, 14]
    assert len(bot.sent_payloads) == 8
    assert [j.status for j in queue.state.jobs] == ["confirmed", "confirmed"]
    assert [j.random_ids for j in queue_at(tmp_path).state.jobs] == original_ids


@pytest.mark.parametrize("change", ["member", "order", "caption", "subset"])
def test_changed_album_is_kept_whole_and_in_order(tmp_path: Path, change: str) -> None:
    first = [forwarded(i, kind="document", group=7, source_post=100 + i) for i in (1, 2, 3)]
    repeated = [forwarded_again(m, m.id + 3, group=8) for m in first]
    if change == "member":
        repeated[1].media.document.id += 1000
    elif change == "order":
        repeated[0].id, repeated[1].id = repeated[1].id, repeated[0].id
    elif change == "caption":
        repeated[1].message = "edited caption"
    else:
        repeated.pop()
    queue = queue_at(tmp_path)
    for message in first + repeated:
        queue.enqueue(message)
    bot = FakeBot(first + repeated)
    process_all(tmp_path, bot, queue)
    assert all(j.status == "confirmed" for j in queue.state.jobs)
    assert len(bot.sent_payloads) == len(first) + len(repeated)
    assert [n for kind, n in bot.timeline if kind == "download"] == [
        *[m.id for m in first],
        *sorted(m.id for m in repeated),
    ]


@pytest.mark.parametrize(
    "change", ["text", "entities", "media", "attributes", "spoiler", "position", "source", "post"]
)
def test_source_or_content_changes_create_a_new_backup(tmp_path: Path, change: str) -> None:
    first = forwarded(1, kind="document", source_post=900)
    second = forwarded_again(first, 2)
    if change == "text":
        second.message = "new text"
    elif change == "entities":
        second.entities = [types.MessageEntityItalic(0, 2)]
    elif change == "media":
        second.media.document.id += 1
    elif change == "attributes":
        second.media.document.attributes[0].file_name = "edited.mp4"
    elif change == "spoiler":
        second.media.spoiler = True
    elif change == "position":
        second.invert_media = True
    elif change == "source":
        second.fwd_from.from_id = types.PeerChannel(888)
    else:
        second.fwd_from.channel_post += 1
    queue = queue_at(tmp_path)
    for message in (first, second):
        queue.enqueue(message)
    bot = FakeBot([first, second])
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 2


@pytest.mark.parametrize("origin", ["missing_post", "hidden_user"])
def test_uncertain_source_never_uses_content_only_deduplication(
    tmp_path: Path, origin: str
) -> None:
    first = forwarded(1, kind="document")
    if origin == "hidden_user":
        first.fwd_from = types.MessageFwdHeader(date=first.date, from_name="Same display name")
    second = forwarded_again(first, 2)
    queue = queue_at(tmp_path)
    for message in (first, second):
        queue.enqueue(message)
    bot = FakeBot([first, second])
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 2


def test_new_forward_and_old_deferred_task_keep_separate_send_ids(tmp_path: Path) -> None:
    first = forwarded(1, kind="document", source_post=900)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    pending_ids = queue.state.jobs[0].random_ids
    queue.defer_pending()
    queue.enqueue(forwarded_again(first, 2))
    bot = FakeBot()
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 1
    assert [j.status for j in queue.state.jobs] == ["queued", "confirmed"]
    restored = queue_at(tmp_path)
    resumed = FakeBot()
    resumed.delivered = dict(bot.delivered)
    process_all(tmp_path, resumed, restored)
    assert len(resumed.delivered) == 2
    assert [n for kind, n in resumed.timeline if kind == "download"] == [1]
    assert restored.state.jobs[0].random_ids == pending_ids
    assert restored.state.jobs[0].status == "confirmed"
    assert restored.next_job() is None


def test_uncertain_final_send_is_never_discarded_as_a_duplicate(tmp_path: Path) -> None:
    first = forwarded(1, source_post=900)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded_again(first, 2))
    job = queue.next_job()
    assert job is not None
    queue.begin(job)
    queue.sending(job.key)
    bot = FakeBot()
    process_all(tmp_path, bot, queue)
    assert tuple(bot.delivered) == job.random_ids


def test_legacy_confirmed_jobs_stay_confirmed_but_allow_new_forwards(tmp_path: Path) -> None:
    first = forwarded(1, source_post=900)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.confirm("message-1", (100,))
    raw = json.loads(queue.path.read_text())
    raw.pop("discarded_through")
    for job in raw["jobs"]:
        job.pop("duplicate_of")
        job.pop("abandoned_from")
        job.pop("cleanup_pending")
    queue.path.write_text(json.dumps(raw))
    restored = queue_at(tmp_path)
    restored.enqueue(forwarded_again(first, 2))
    bot = FakeBot()
    process_all(tmp_path, bot, restored)
    assert len(bot.delivered) == 1
    assert restored.state.jobs[0].output_ids == [100]
    assert restored.state.jobs[1].status == "confirmed"


def test_failed_legacy_recovery_checkpoint_preserves_both_memory_and_disk(
    tmp_path: Path, monkeypatch
) -> None:
    first = forwarded(1, source_post=900)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded_again(first, 2))
    queue.update(
        queue.get("message-2").model_copy(
            update={"status": "duplicate", "duplicate_of": "message-1"}
        )
    )
    before = queue.path.read_bytes()

    def fail(*args, **kwargs):
        raise AppError("disk full")

    monkeypatch.setattr("tg_comment_uploader.reupload_queue.write_private_json", fail)
    with pytest.raises(AppError, match="disk full"):
        queue.restore_legacy_skips()
    assert queue.path.read_bytes() == before
    assert queue.state.jobs[-1].status == "duplicate"


def test_identical_formatted_date_messages_can_be_forwarded_twice(tmp_path: Path) -> None:
    first = forwarded(1, source_post=900)
    first.entities = [types.MessageEntityFormattedDate(offset=0, length=1, date=first.date)]
    second = forwarded_again(first, 2)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.enqueue(second)
    bot = FakeBot()
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 2


def test_two_interruptions_retry_one_input_without_creating_new_backup_ids(tmp_path: Path) -> None:
    message = forwarded(1, kind="document", source_post=900)
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    ids = queue.state.jobs[0].random_ids
    delivered = {}
    for attempt in range(3):
        restored = queue_at(tmp_path)
        assert not restored.enqueue(message)
        bot = FakeBot()
        bot.delivered = delivered
        bot.fail_final = int(attempt < 2)
        if attempt < 2:
            with pytest.raises(AppError, match="rerun just reupload"):
                process_all(tmp_path, bot, restored)
            assert queue_at(tmp_path).state.jobs[0].status == "sending"
        else:
            process_all(tmp_path, bot, restored)
        assert tuple(bot.delivered) == ids
        assert len(restored.state.jobs) == 1
        if attempt:
            assert not any(kind == "download" for kind, _ in bot.timeline)
        delivered = dict(bot.delivered)
    completed = queue_at(tmp_path)
    assert completed.state.jobs[0].status == "confirmed"
    assert not completed.enqueue(message)
    final = FakeBot()
    process_all(tmp_path, final, completed)
    assert not final.requests
