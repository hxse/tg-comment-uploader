from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import MemorySession

from reupload_fakes import FakeBot, queue_at
from test_reupload_delivery import process_all
from test_reupload_download_resume import (
    CHUNK,
    PAYLOAD,
    file_path,
    interrupt_download,
    message_with_payload,
)
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_transfer import download_media


@pytest.mark.parametrize(
    "change", ["tamper", "truncate", "too-large", "wrong-source", "invalid-json"]
)
def test_invalid_partial_data_stops_before_any_new_download_or_send(tmp_path: Path, change: str):
    _, queue, _ = interrupt_download(tmp_path)
    path = file_path(queue).with_suffix(".bin.part")
    checkpoint = path.with_suffix(".part.json")
    if change == "tamper":
        with path.open("r+b") as output:
            output.write(b"wrong")
    elif change == "truncate":
        with path.open("r+b") as output:
            output.truncate(3)
    elif change == "too-large":
        with path.open("r+b") as output:
            output.truncate(len(PAYLOAD) + 1)
    elif change == "wrong-source":
        saved = json.loads(checkpoint.read_text())
        saved["identity"] = "different-file"
        checkpoint.write_text(json.dumps(saved))
    else:
        checkpoint.write_text("invalid JSON")
    bot = FakeBot([])
    bot.payload = PAYLOAD
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue_at(tmp_path))
    assert not bot.timeline
    assert not bot.requests
    assert queue_at(tmp_path).state.jobs[1].status == "queued"


@pytest.mark.parametrize("target", ["partial", "checkpoint"])
def test_partial_download_refuses_symlinks(tmp_path: Path, target: str):
    _, queue, _ = interrupt_download(tmp_path)
    partial = file_path(queue).with_suffix(".bin.part")
    path = partial if target == "partial" else partial.with_suffix(".part.json")
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    original = outside.read_bytes()
    path.unlink()
    path.symlink_to(outside)
    bot = FakeBot([])
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue_at(tmp_path))
    assert outside.read_bytes() == original
    assert not bot.requests


def test_remote_overlap_must_match_the_saved_prefix(tmp_path: Path):
    _, queue, _ = interrupt_download(tmp_path)
    partial = file_path(queue).with_suffix(".bin.part")
    prefix = partial.read_bytes()
    bot = FakeBot([])
    bot.payload = PAYLOAD[:CHUNK] + b"xxxx" + PAYLOAD[CHUNK + 4 :]
    with pytest.raises(AppError, match="remote media differs"):
        process_all(tmp_path, bot, queue_at(tmp_path))
    assert partial.read_bytes() == prefix
    assert not bot.sent_payloads


def test_failed_checkpoint_never_publishes_a_file(tmp_path: Path, monkeypatch):
    _, queue, _ = interrupt_download(tmp_path)
    bot = FakeBot([])
    bot.payload = PAYLOAD

    def disk_full(*args, **kwargs):
        raise AppError("checkpoint disk full")

    monkeypatch.setattr("tg_comment_uploader.reupload_partial.write_private_json", disk_full)
    with pytest.raises(AppError, match="disk full"):
        process_all(tmp_path, bot, queue_at(tmp_path))
    assert not bot.requests
    assert not file_path(queue).exists()


def test_completed_but_uncheckpointed_file_is_verified_before_reuse(tmp_path: Path, monkeypatch):
    message = message_with_payload()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.payload = PAYLOAD

    def fail(*args):
        raise AppError("queue write failed")

    monkeypatch.setattr(queue, "downloaded", fail)
    with pytest.raises(AppError, match="queue write failed"):
        process_all(tmp_path, bot, queue)
    path = file_path(queue)
    with path.open("r+b") as output:
        output.write(b"modified")
    resumed = FakeBot([])
    with pytest.raises(AppError, match="completed download changed"):
        process_all(tmp_path, resumed, queue_at(tmp_path))
    assert not resumed.requests


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_real_telethon_offset_iterator_uses_snapshot_reference_and_correct_photo_size(
    tmp_path: Path, monkeypatch, kind: str
):
    message = message_with_payload(kind)
    path = tmp_path / "media.bin"
    path.with_suffix(".bin.part").write_bytes(PAYLOAD[: CHUNK + 17])
    offsets = []

    async def scenario():
        session = MemorySession()
        session.set_dc(2, "127.0.0.1", 443)
        client = TelegramClient(session, 123, "fake-api-hash")

        async def get_file(sender, request):
            assert isinstance(request, functions.upload.GetFileRequest)
            assert request.location.id == 377
            assert request.location.file_reference == (b"ref" if kind == "photo" else b"source-ref")
            assert request.location.thumb_size == ("x" if kind == "photo" else "")
            offsets.append(request.offset)
            return types.upload.File(
                type=types.storage.FileUnknown(),
                mtime=0,
                bytes=PAYLOAD[request.offset : request.offset + request.limit],
            )

        monkeypatch.setattr(client, "_call", get_file)
        monkeypatch.setattr(
            client, "get_messages", AsyncMock(side_effect=AssertionError("no refetch"))
        )
        try:
            size, _ = await download_media(client, message, path)
            assert size == len(PAYLOAD)
        finally:
            await client.disconnect()

    asyncio.run(scenario())
    assert offsets[0] == CHUNK
    assert all(offset >= CHUNK for offset in offsets)
    assert path.read_bytes() == PAYLOAD
    if kind == "photo":
        assert isinstance(message.media, types.MessageMediaPhoto)
        assert isinstance(message.media.photo, types.Photo)
        assert len(message.media.photo.sizes) == 2  # The snapshot was not mutated.


def test_photo_bytes_embedded_in_snapshot_need_no_network(tmp_path: Path):
    message = message_with_payload("photo")
    assert isinstance(message.media, types.MessageMediaPhoto)
    assert isinstance(message.media.photo, types.Photo)
    message.media.photo.sizes = [types.PhotoCachedSize("x", 320, 240, PAYLOAD)]
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([])
    process_all(tmp_path, bot, queue)
    assert bot.download_offsets == []
    assert bot.sent_payloads == [PAYLOAD]


@pytest.mark.parametrize("failure", [ConnectionError("media-DC failure"), asyncio.CancelledError()])
def test_iterator_initialization_failure_preserves_original_error(tmp_path, monkeypatch, failure):
    async def scenario():
        session = MemorySession()
        session.set_dc(1, "127.0.0.1", 443)
        client = TelegramClient(session, 123, "fake-api-hash")
        monkeypatch.setattr(client, "_borrow_exported_sender", AsyncMock(side_effect=failure))
        try:
            with pytest.raises(type(failure)) as caught:
                await download_media(client, message_with_payload(), tmp_path / "media.bin")
            assert caught.value is failure
        finally:
            await client.disconnect()

    asyncio.run(scenario())
    checkpoint = json.loads((tmp_path / "media.bin.part.json").read_text())
    assert checkpoint["size"] == 0
    assert not (tmp_path / "media.bin").exists()
