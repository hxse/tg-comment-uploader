from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from telethon import types

from reupload_fakes import BOT, CHAT, PEER, forwarded, queue_at
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_message import accepts_forward, decode_message, encode_message
from tg_comment_uploader.reupload_queue import DownloadedFile


def test_bulk_and_album_are_sorted_and_restored_with_the_same_random_ids(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    for message in [
        forwarded(12, group=3, kind="photo"),
        forwarded(15),
        forwarded(10),
        forwarded(11, group=3, kind="document"),
    ]:
        queue.enqueue(message, now=0.0)
    assert [[m.message_id for m in j.messages] for j in queue.state.jobs] == [[10], [11, 12], [15]]
    before = [j.random_ids for j in queue.state.jobs]
    first = queue.next_job()
    assert first is not None
    queue.begin(first)
    queue.sending(first.key)
    queue.confirm(first.key, (100,))
    restored = queue_at(tmp_path)
    assert [j.random_ids for j in restored.state.jobs] == before
    next_job = restored.next_job()
    assert next_job is not None and next_job.first_id == 11
    assert not restored.enqueue(forwarded(10))
    assert not restored.enqueue(forwarded(12, group=3, kind="photo"))
    if os.name == "posix":
        assert stat.S_IMODE(queue.path.stat().st_mode) == 0o600


def test_failed_atomic_save_does_not_advance_in_memory(tmp_path: Path, monkeypatch) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))

    def fail(*args, **kwargs):
        raise AppError("disk full")

    monkeypatch.setattr("tg_comment_uploader.reupload_queue.write_private_json", fail)
    with pytest.raises(AppError, match="disk full"):
        queue.enqueue(forwarded(2))
    assert [j.first_id for j in queue.state.jobs] == [1]
    assert [j.first_id for j in queue_at(tmp_path).state.jobs] == [1]


def test_late_album_member_blocks_instead_of_changing_a_started_request(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, group=7, kind="photo"))
    queue.enqueue(forwarded(2, group=7, kind="photo"))
    job = queue.next_job()
    assert job is not None
    queue.begin(job)
    ids = job.random_ids
    queue.enqueue(forwarded(3, group=7, kind="photo"))
    restored = queue_at(tmp_path)
    assert restored.get(job.key).random_ids == ids
    assert (queue.path.parent / "late-3.json").is_file()
    with pytest.raises(AppError, match="late input"):
        restored.sending(job.key)


@pytest.mark.parametrize(
    "change", ["foreign-chat", "bad-snapshot", "duplicate-random", "unconfirmed"]
)
def test_corrupt_state_stops_before_any_send(tmp_path: Path, change: str) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.enqueue(forwarded(2))
    raw = json.loads(queue.path.read_text())
    if change == "foreign-chat":
        raw["chat_id"] = 99
    elif change == "bad-snapshot":
        raw["jobs"][0]["messages"][0]["snapshot"] = "not valid base64"
    elif change == "duplicate-random":
        raw["jobs"][1]["messages"][0]["random_id"] = raw["jobs"][0]["messages"][0]["random_id"]
    else:
        raw["jobs"][0]["status"] = "confirmed"
    queue.path.write_text(json.dumps(raw))
    with pytest.raises(AppError):
        queue_at(tmp_path)


def test_snapshot_preserves_utf16_entities_and_video_attributes(tmp_path: Path) -> None:
    original = forwarded(1, kind="document")
    original.entities = [
        types.MessageEntityBold(0, 2),
        types.MessageEntityTextUrl(3, 2, "https://x.test"),
    ]
    restored = decode_message(encode_message(original))
    assert bytes(restored) == bytes(original)
    queue = queue_at(tmp_path)
    queue.enqueue(original)
    job = queue.next_job()
    assert job is not None
    queue.downloaded(job.key, 1, DownloadedFile(size=12, sha256="a" * 64))
    restored = queue_at(tmp_path).next_job()
    assert restored is not None
    downloaded = restored.messages[0].downloaded
    assert downloaded is not None and downloaded.size == 12


def test_intake_filters_chat_direction_and_own_copies() -> None:
    channel = forwarded(1)
    channel.out = True  # channel admin posts can have out=True
    assert accepts_forward(channel, CHAT, BOT)
    assert not accepts_forward(channel, CHAT - 1, BOT)
    assert not accepts_forward(
        types.Message(2, PEER, date=None, message="copy", out=True), CHAT, BOT
    )
    private = forwarded(3)
    private.peer_id = types.PeerUser(42)
    private.from_id = types.PeerUser(42)
    assert accepts_forward(private, 42, BOT)
    private.out = True
    assert not accepts_forward(private, 42, BOT)
    private.out = False
    private.from_id = types.PeerUser(BOT)
    assert not accepts_forward(private, 42, BOT)


def test_interleaved_album_and_message_stop_before_upload(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    for message in [
        forwarded(1, kind="photo", group=7),
        forwarded(2),
        forwarded(3, kind="photo", group=7),
    ]:
        queue.enqueue(message)
    job = queue.next_job()
    assert job is not None
    with pytest.raises(AppError, match="interleaved"):
        queue.begin(job)
    assert all(j.status == "queued" for j in queue.state.jobs)


def test_input_message_id_cannot_confirm_a_new_backup(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    job = queue.next_job()
    assert job is not None
    queue.begin(job)
    queue.sending(job.key)
    with pytest.raises(AppError, match="invalid Telegram confirmation"):
        queue.confirm(job.key, (1,))
    assert queue_at(tmp_path).state.jobs[0].status == "sending"
