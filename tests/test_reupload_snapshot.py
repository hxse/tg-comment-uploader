from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from telethon import TelegramClient, errors, types
from telethon.sessions import MemorySession

from reupload_fakes import FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_message import decode_message
from tg_comment_uploader.reupload_queue import DownloadReference
from tg_comment_uploader.reupload_source import REFERENCE_ERRORS, snapshot_source


def media_object(message: types.Message) -> types.Document | types.Photo:
    if isinstance(message.media, types.MessageMediaPhoto):
        assert isinstance(message.media.photo, types.Photo)
        return message.media.photo
    assert isinstance(message.media, types.MessageMediaDocument)
    assert isinstance(message.media.document, types.Document)
    return message.media.document


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_deleted_album_downloads_from_saved_snapshots_without_message_lookups(
    tmp_path: Path, kind: str
) -> None:
    messages = [forwarded(i, kind=kind, group=7) for i in (5, 6, 7, 8)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    snapshots = [m.snapshot for m in queue.state.jobs[0].messages]
    # Simulate restart after the user has deleted every original post in B.
    bot = FakeBot([])
    restored = queue_at(tmp_path)
    process_all(tmp_path, bot, restored)
    assert bot.message_lookups == []
    assert [n for kind, n in bot.timeline if kind == "download"] == [5, 6, 7, 8]
    assert bot.sent_payloads == [bot.payload] * 4
    assert [m.snapshot for m in restored.state.jobs[0].messages] == snapshots
    assert restored.state.jobs[0].status == "confirmed"


def test_deleting_inputs_during_first_download_does_not_block_later_members(tmp_path: Path) -> None:
    messages = [forwarded(5, kind="document", group=7), forwarded(6, kind="photo", group=7)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    bot = FakeBot(messages)

    async def remove_originals(message):
        bot.sources.clear()

    bot.download_hook = remove_originals
    process_all(tmp_path, bot, queue)
    assert bot.message_lookups == []
    assert len(bot.delivered) == 2


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_refresh_changes_only_download_reference_and_preserves_original_content(
    tmp_path: Path, kind: str
) -> None:
    initial = forwarded(5, kind=kind, text="initial caption")
    fresh = forwarded(5, kind=kind, text="edited caption")
    fresh.entities = [types.MessageEntityItalic(0, 2)]
    fresh_media = media_object(fresh)
    fresh_media.file_reference = b"refreshed-reference"
    fresh_media.access_hash = 999
    queue = queue_at(tmp_path)
    queue.enqueue(initial)
    saved = queue.state.jobs[0].messages[0]
    bot = FakeBot([fresh])

    async def require_new_reference(message):
        media = message.media.photo if kind == "photo" else message.media.document
        if media.file_reference != b"refreshed-reference":
            raise errors.FileReferenceExpiredError(request=None)
        assert media.access_hash == 999
        assert message.message == "initial caption"
        assert isinstance(message.entities[0], types.MessageEntityBold)
        assert message.input_chat is None

    bot.download_hook = require_new_reference
    process_all(tmp_path, bot, queue)
    assert bot.message_lookups == [5]
    assert bot.sent_text == ["initial caption"]
    restored = queue_at(tmp_path).state.jobs[0].messages[0]
    assert restored.snapshot == saved.snapshot and restored.random_id == saved.random_id
    assert restored.download_reference == DownloadReference(
        access_hash=999,
        file_reference=base64.b64encode(b"refreshed-reference").decode("ascii"),
    )


def test_refreshed_reference_survives_interruption_then_source_deletion(tmp_path: Path) -> None:
    original = forwarded(5, kind="document")
    fresh = forwarded(5, kind="document")
    media_object(fresh).file_reference = b"new-reference"
    queue = queue_at(tmp_path)
    queue.enqueue(original)
    bot = FakeBot([fresh])

    async def expire_then_interrupt(message):
        if message.media.document.file_reference != b"new-reference":
            raise errors.FileReferenceExpiredError(request=None)
        raise ConnectionError("download interrupted after refreshing the reference")

    bot.download_hook = expire_then_interrupt
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    resumed = FakeBot([])

    async def cached_reference_only(message):
        assert message.media.document.file_reference == b"new-reference"

    resumed.download_hook = cached_reference_only
    restored = queue_at(tmp_path)
    process_all(tmp_path, resumed, restored)
    assert resumed.message_lookups == []
    assert restored.next_job() is None


@pytest.mark.parametrize("error_type", REFERENCE_ERRORS)
def test_unusable_reference_and_deleted_source_report_unrecoverable_media(
    tmp_path: Path, error_type
) -> None:
    original = forwarded(5, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(original)
    queue.enqueue(forwarded(6))
    bot = FakeBot([])

    async def invalid_reference(message):
        raise error_type(request=None)

    bot.download_hook = invalid_reference
    with pytest.raises(AppError, match="metadata alone"):
        process_all(tmp_path, bot, queue)
    assert bot.message_lookups == [5]
    assert not bot.delivered
    assert queue.state.jobs[1].status == "queued"


def test_refresh_rejects_replaced_media_without_updating_initial_snapshot(tmp_path: Path) -> None:
    original = forwarded(5, kind="document")
    replacement = forwarded(5, kind="document")
    media_object(replacement).id = 999
    queue = queue_at(tmp_path)
    queue.enqueue(original)
    saved = queue.state.jobs[0].messages[0]
    bot = FakeBot([replacement])

    async def expired(message):
        raise errors.FileReferenceExpiredError(request=None)

    bot.download_hook = expired
    with pytest.raises(AppError, match="media changed"):
        process_all(tmp_path, bot, queue)
    assert queue.state.jobs[0].messages[0] == saved
    assert len(bot.timeline) == 1 and not bot.delivered


def test_still_invalid_after_refresh_stops_instead_of_refetching_forever(tmp_path: Path) -> None:
    message = forwarded(5, kind="photo")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])

    async def always_invalid(message):
        raise errors.FileReferenceInvalidError(request=None)

    bot.download_hook = always_invalid
    with pytest.raises(AppError, match="rejected the refreshed media reference"):
        process_all(tmp_path, bot, queue)
    assert bot.message_lookups == [5] and len(bot.timeline) == 2


@pytest.mark.parametrize("kind", ["photo", "document"])
def test_real_telethon_uses_snapshot_location_without_a_message_refresh(
    tmp_path: Path, kind: str, monkeypatch
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(5, kind=kind))
    original = decode_message(queue.state.jobs[0].messages[0].snapshot)
    source = snapshot_source(
        original,
        DownloadReference(
            access_hash=777,
            file_reference=base64.b64encode(b"cached-reference").decode("ascii"),
        ),
    )

    async def scenario() -> None:
        client = TelegramClient(MemorySession(), 123, "fake-api-hash")
        output = io.BytesIO()

        async def get_file(location, file, **kwargs):
            assert location.id == 5 and location.access_hash == 777
            assert location.file_reference == b"cached-reference"
            assert kwargs.get("msg_data") is None
            file.write(b"media bytes")
            return file

        async def forbidden(*args, **kwargs):
            raise AssertionError("snapshot download must not refetch the message")

        monkeypatch.setattr(client, "_download_file", get_file)
        monkeypatch.setattr(client, "get_messages", forbidden)
        await client.download_media(source, file=output)
        assert output.getvalue() == b"media bytes"
        await client.disconnect()

    asyncio.run(scenario())


def test_older_queue_schema_without_cached_reference_is_still_readable(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(5, kind="document"))
    raw = json.loads(queue.path.read_text())
    del raw["jobs"][0]["messages"][0]["download_reference"]
    queue.path.write_text(json.dumps(raw))
    assert queue_at(tmp_path).state.jobs[0].messages[0].download_reference is None


def test_corrupt_cached_reference_fails_closed(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(5, kind="document"))
    raw = json.loads(queue.path.read_text())
    raw["jobs"][0]["messages"][0]["download_reference"] = {
        "access_hash": 123,
        "file_reference": "not base64!",
    }
    queue.path.write_text(json.dumps(raw))
    with pytest.raises(AppError, match="invalid reupload queue"):
        queue_at(tmp_path)


def test_local_snapshot_ready_is_reported_only_after_all_downloads(tmp_path: Path, capsys) -> None:
    messages = [forwarded(5, kind="document", group=7), forwarded(6, kind="photo", group=7)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    bot = FakeBot([])

    async def before_download_finishes(message):
        assert "local snapshot ready" not in capsys.readouterr().out

    def before_send(request):
        assert all(m.downloaded is not None for m in queue.state.jobs[0].messages)
        assert "local snapshot ready for inputs 5,6" in capsys.readouterr().out

    bot.download_hook = before_download_finishes
    bot.send_hook = before_send
    process_all(tmp_path, bot, queue)
