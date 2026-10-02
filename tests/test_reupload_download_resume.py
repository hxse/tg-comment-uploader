from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from pathlib import Path

import pytest
from telethon import errors, types

from reupload_fakes import FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import job_directory
from tg_comment_uploader.reupload_partial import PartialDownload
from tg_comment_uploader.reupload_transfer import DOWNLOAD_CHUNK_BYTES as CHUNK

PAYLOAD = bytes(range(256)) * (3 * CHUNK // 256) + b"final bytes"


def message_with_payload(kind="document"):
    message = forwarded(377, kind=kind, payload=PAYLOAD)
    if kind == "photo":
        assert isinstance(message.media, types.MessageMediaPhoto)
        assert isinstance(message.media.photo, types.Photo)
        message.media.photo.sizes = [
            types.PhotoSize("x", 1920, 1080, len(PAYLOAD)),
            types.PhotoStrippedSize("i", b"preview"),
        ]
    return message


def file_path(queue, kind="document"):
    suffix = "media.jpg" if kind == "photo" else "media.bin"
    return job_directory(queue, queue.state.jobs[0]) / "377" / suffix


def interrupt_download(tmp_path, *, kind="document", cancel=False):
    message = message_with_payload(kind)
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(forwarded(378, text="following message"))
    bot = FakeBot([message])
    bot.payload = PAYLOAD

    async def interrupt(message, offset):
        if offset >= CHUNK + 4:
            if cancel:
                raise asyncio.CancelledError
            raise ConnectionError("lost stream")

    bot.download_chunk_hook = interrupt
    with pytest.raises(asyncio.CancelledError if cancel else AppError):
        process_all(tmp_path, bot, queue)
    assert bot.download_streams_closed == 1
    assert not bot.sent_payloads
    assert queue.state.jobs[1].status == "queued"
    return message, queue, bot


@pytest.mark.parametrize("kind", ["document", "photo"])
@pytest.mark.parametrize("cancel", [False, True])
def test_restart_resumes_partial_bytes_preserving_payload_order_and_send_ids(
    tmp_path: Path, kind: str, cancel: bool, capsys
) -> None:
    message, queue, _ = interrupt_download(tmp_path, kind=kind, cancel=cancel)
    path = file_path(queue, kind)
    partial = path.with_suffix(path.suffix + ".part")
    prefix = PAYLOAD[: CHUNK + 4]
    assert partial.read_bytes() == prefix
    checkpoint = json.loads(partial.with_suffix(partial.suffix + ".json").read_text())
    assert checkpoint["size"] == len(prefix)
    assert checkpoint["sha256"] == hashlib.sha256(prefix).hexdigest()
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    ids = [job.random_ids for job in queue.state.jobs]
    saved_snapshot = queue.state.jobs[0].messages[0].snapshot

    # Deleted source messages do not prevent use of the saved media reference.
    resumed = FakeBot([])
    resumed.payload = PAYLOAD
    process_all(tmp_path, resumed, queue_at(tmp_path))
    assert resumed.download_offsets == [(377, CHUNK)]
    assert resumed.download_streams_closed == 1
    assert resumed.sent_payloads == [PAYLOAD]
    assert resumed.sent_text == [message.message, "following message"]
    restored = queue_at(tmp_path)
    assert restored.next_job() is None
    assert [job.random_ids for job in restored.state.jobs] == ids
    assert restored.state.jobs[0].messages[0].snapshot == saved_snapshot
    assert path.read_bytes() == PAYLOAD
    assert not partial.exists()
    assert "resuming input 377 from" in capsys.readouterr().out


@pytest.mark.parametrize("prefix_size", [CHUNK, CHUNK + 17, len(PAYLOAD)])
def test_legacy_partial_without_checkpoint_is_adopted(tmp_path: Path, prefix_size: int) -> None:
    message = message_with_payload()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    path = file_path(queue)
    path.parent.mkdir(parents=True)
    path.with_suffix(".bin.part").write_bytes(PAYLOAD[:prefix_size])
    bot = FakeBot([])
    bot.payload = PAYLOAD
    process_all(tmp_path, bot, queue_at(tmp_path))
    assert bot.sent_payloads == [PAYLOAD]
    expected = [] if prefix_size == len(PAYLOAD) else [(377, prefix_size // CHUNK * CHUNK)]
    assert bot.download_offsets == expected
    assert queue_at(tmp_path).next_job() is None


def test_uncheckpointed_crash_tail_is_discarded_without_redownloading_saved_prefix(
    tmp_path: Path,
) -> None:
    message, queue, _ = interrupt_download(tmp_path)
    path = file_path(queue)
    partial = path.with_suffix(".bin.part")
    with partial.open("ab") as output:
        output.write(b"uncheckpointed tail")
    bot = FakeBot([message])
    bot.payload = PAYLOAD
    process_all(tmp_path, bot, queue_at(tmp_path))
    assert bot.download_offsets == [(377, CHUNK)]
    assert bot.sent_payloads == [PAYLOAD]


def test_completed_rename_before_queue_checkpoint_is_recovered_without_download(
    tmp_path: Path, monkeypatch
) -> None:
    message = message_with_payload()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.payload = PAYLOAD

    def fail(*args):
        raise AppError("queue checkpoint interrupted")

    monkeypatch.setattr(queue, "downloaded", fail)
    with pytest.raises(AppError, match="queue checkpoint interrupted"):
        process_all(tmp_path, bot, queue)
    assert file_path(queue).read_bytes() == PAYLOAD
    resumed = FakeBot([])
    process_all(tmp_path, resumed, queue_at(tmp_path))
    assert resumed.download_offsets == []
    assert resumed.sent_payloads == [PAYLOAD]


def test_expired_reference_refresh_keeps_partial_bytes_and_original_caption(tmp_path: Path) -> None:
    original, queue, _ = interrupt_download(tmp_path)
    fresh = message_with_payload()
    fresh.message = "edited caption"
    fresh.media.document.file_reference = b"fresh-reference"
    bot = FakeBot([fresh])
    bot.payload = PAYLOAD

    async def require_fresh(message):
        if message.media.document.file_reference != b"fresh-reference":
            raise errors.FileReferenceExpiredError(request=None)

    bot.download_hook = require_fresh
    process_all(tmp_path, bot, queue_at(tmp_path))
    assert bot.message_lookups == [377]
    assert bot.download_offsets == [(377, CHUNK), (377, CHUNK)]
    assert bot.sent_text == [original.message, "following message"]
    assert bot.sent_payloads == [PAYLOAD]


def test_successful_cleanup_removes_partial_checkpoint_too(tmp_path: Path) -> None:
    _, queue, _ = interrupt_download(tmp_path)
    folder = job_directory(queue, queue.state.jobs[0])
    bot = FakeBot([])
    bot.payload = PAYLOAD
    process_all(tmp_path, bot, queue_at(tmp_path), keep=False)
    assert not folder.exists()
    assert queue_at(tmp_path).next_job() is None


def test_periodic_checkpoint_covers_only_durable_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("tg_comment_uploader.reupload_partial.CHECKPOINT_BYTES", 8)
    path = tmp_path / "file.bin"
    partial = PartialDownload(path, identity="test-source", total_size=32)
    with partial:
        asyncio.run(partial.restore())
        partial.write(b"first---")
        saved = json.loads(partial.checkpoint_path.read_text())
        assert saved["size"] == 8
        assert saved["sha256"] == hashlib.sha256(b"first---").hexdigest()
        partial.write(b"last")
        assert json.loads(partial.checkpoint_path.read_text())["size"] == 8
    saved = json.loads(partial.checkpoint_path.read_text())
    assert saved["size"] == 12
    assert saved["sha256"] == hashlib.sha256(b"first---last").hexdigest()
