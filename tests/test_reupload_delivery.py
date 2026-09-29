from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from telethon import errors, functions, types

from reupload_fakes import BOT, FakeBot, forwarded, queue_at, sender_at
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import job_directory
from tg_comment_uploader.reupload_queue import ReuploadQueue
from tg_comment_uploader.reupload_service import ReuploadService


def process_all(tmp_path: Path, bot: FakeBot, queue: ReuploadQueue, *, keep: bool = True) -> None:
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=keep)
    bot.handler = service.on_message
    with sender_at(tmp_path, bot) as sender:

        async def work() -> None:
            client, peer = await sender.connect()
            while (job := queue.next_job()) is not None:
                await service.process_job(sender, client, peer, job)

        sender.run_service(work())


def test_mixed_album_downloads_all_bytes_then_uploads_in_order_and_does_not_loop(
    tmp_path: Path,
) -> None:
    inputs = [
        forwarded(1, text="纯文字 😀"),
        forwarded(2, kind="photo", group=7, text="图片"),
        forwarded(3, kind="document", group=7, text="视频"),
        forwarded(4, kind="document"),
    ]
    photo_media = inputs[1].media
    assert isinstance(photo_media, types.MessageMediaPhoto)
    photo_media.spoiler = True
    inputs[1].invert_media = inputs[2].invert_media = True
    queue = queue_at(tmp_path)
    for message in reversed(inputs):
        queue.enqueue(message)
    bot = FakeBot(inputs)

    def check_checkpoint(request):
        saved = queue_at(tmp_path).next_job()
        assert saved is not None and saved.status == "sending"
        ids = (
            [m.random_id for m in request.multi_media]
            if isinstance(request, functions.messages.SendMultiMediaRequest)
            else [request.random_id]
        )
        assert list(saved.random_ids) == ids

    bot.send_hook = check_checkpoint
    process_all(tmp_path, bot, queue)
    assert [kind for kind, _ in bot.timeline] == [
        "send",
        "download",
        "download",
        "send",
        "download",
        "send",
    ]
    assert [value for kind, value in bot.timeline if kind == "download"] == [2, 3, 4]
    assert bot.sent_payloads == [bot.payload] * 3
    assert bot.sent_text == [m.message for m in inputs]
    assert len(queue.state.jobs) == 3  # own updates did not become new inputs
    assert all(j.status == "confirmed" for j in queue_at(tmp_path).state.jobs)
    album = next(r for r in bot.requests if isinstance(r, functions.messages.SendMultiMediaRequest))
    assert album.invert_media is True
    assert isinstance(album.multi_media[0].media, types.InputMediaPhoto)
    assert album.multi_media[0].media.spoiler is True
    assert isinstance(album.multi_media[1].media, types.InputMediaDocument)
    assert [bytes(e) for e in album.multi_media[0].entities] == [
        bytes(e) for e in inputs[1].entities or ()
    ]
    materialized_video = next(
        r
        for r in bot.requests
        if isinstance(r, functions.messages.UploadMediaRequest)
        and isinstance(r.media, types.InputMediaUploadedDocument)
    )
    assert materialized_video.media.mime_type == "video/mp4"
    original_media = inputs[2].media
    assert isinstance(original_media, types.MessageMediaDocument)
    assert isinstance(original_media.document, types.Document)
    assert [bytes(a) for a in materialized_video.media.attributes] == [
        bytes(a) for a in original_media.document.attributes
    ]
    metadata = job_directory(queue, queue.state.jobs[0]) / "1" / "message.json"
    assert json.loads(metadata.read_text())["text"] == inputs[0].message


def test_single_photo_uses_fresh_local_upload_and_preserves_caption(tmp_path: Path) -> None:
    message = forwarded(1, kind="photo")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    process_all(tmp_path, bot, queue)
    request = next(r for r in bot.requests if isinstance(r, functions.messages.SendMediaRequest))
    assert isinstance(request.media, types.InputMediaUploadedPhoto)
    assert bot.file_bytes(request.media.file) == bot.payload
    assert request.message == message.message
    assert [bytes(e) for e in request.entities] == [bytes(e) for e in message.entities or ()]


def test_lost_final_response_resumes_with_identical_ids_and_skips_confirmed(tmp_path: Path) -> None:
    messages = [forwarded(1, kind="document"), forwarded(2, kind="photo")]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    ids = [j.random_ids for j in queue.state.jobs]
    bot = FakeBot(messages)
    bot.fail_final = 1
    with pytest.raises(AppError, match="rerun just reupload"):
        process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 1
    restored = queue_at(tmp_path)
    pending = restored.next_job()
    assert pending is not None and pending.status == "sending"
    assert [j.random_ids for j in restored.state.jobs] == ids
    # Source 1 is now unavailable, but its verified local download is enough.
    resumed = FakeBot([messages[1]])
    resumed.delivered = bot.delivered
    process_all(tmp_path, resumed, restored)
    assert len(resumed.delivered) == 2
    assert [n for kind, n in resumed.timeline if kind == "download"] == [2]
    assert restored.state.jobs[0].output_ids == [10000]
    finished = FakeBot([])
    process_all(tmp_path, finished, queue_at(tmp_path))
    assert not finished.requests


def test_interrupted_album_download_reuses_completed_files_and_preserves_all_members(
    tmp_path: Path,
) -> None:
    messages = [forwarded(1, kind="document", group=12), forwarded(2, kind="photo", group=12)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    bot = FakeBot(messages)

    async def fail_second(message):
        if message.id == 2:
            raise ConnectionError("lost download")

    bot.download_hook = fail_second
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    assert not bot.delivered
    restored = queue_at(tmp_path)
    job = restored.next_job()
    assert job is not None
    assert job.messages[0].downloaded is not None and job.messages[1].downloaded is None
    resumed = FakeBot([messages[1]])
    process_all(tmp_path, resumed, restored)
    assert [n for kind, n in resumed.timeline if kind == "download"] == [2]
    assert resumed.sent_payloads == [resumed.payload] * 2
    assert len(resumed.delivered) == 2


@pytest.mark.parametrize("unsupported", [False, True])
def test_expired_reference_or_unsupported_input_stops_before_later_messages(
    tmp_path: Path, unsupported: bool
) -> None:
    first = forwarded(1, kind="document")
    if unsupported:
        first.media = types.MessageMediaContact("123", "first", "last", "", 12)
    second = forwarded(2, kind="photo")
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    queue.enqueue(second)
    bot = FakeBot([first, second] if unsupported else [second])
    if not unsupported:

        async def expired_reference(message):
            raise errors.FileReferenceExpiredError(request=None)

        bot.download_hook = expired_reference
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    assert not bot.delivered
    pending = queue.next_job()
    assert pending is not None and pending.first_id == 1
    assert queue.state.jobs[1].status == "queued"


def test_input_arriving_during_download_is_saved_before_current_send(tmp_path: Path) -> None:
    first, second = forwarded(1, kind="document"), forwarded(2)
    queue = queue_at(tmp_path)
    queue.enqueue(first)
    bot = FakeBot([first])
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)

    async def enqueue_second(message):
        from types import SimpleNamespace

        await service.on_message(SimpleNamespace(message=second))
        assert len(queue_at(tmp_path).state.jobs) == 2

    bot.download_hook = enqueue_second
    process_all(tmp_path, bot, queue)
    assert bot.sent_text == [first.message, second.message]


def test_cannot_send_when_final_checkpoint_fails(tmp_path: Path, monkeypatch) -> None:
    message = forwarded(1, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])

    def fail(key):
        raise AppError("simulated disk full")

    monkeypatch.setattr(queue, "sending", fail)
    with pytest.raises(AppError, match="persist upload state"):
        process_all(tmp_path, bot, queue)
    assert not bot.delivered
    assert not any(isinstance(r, functions.messages.SendMediaRequest) for r in bot.requests)


def test_cancel_at_final_send_keeps_intent_and_does_not_start_next(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    messages = [forwarded(1, kind="document"), forwarded(2)]
    for message in messages:
        queue.enqueue(message)
    bot = FakeBot(messages)

    def cancel(request):
        raise asyncio.CancelledError

    bot.send_hook = cancel
    with pytest.raises(asyncio.CancelledError):
        process_all(tmp_path, bot, queue)
    restored = queue_at(tmp_path)
    assert restored.state.jobs[0].status == "sending"
    assert restored.state.jobs[1].status == "queued"
    assert not bot.delivered


def test_cleanup_only_after_confirmation_retains_deduplication_state(tmp_path: Path) -> None:
    message = forwarded(1, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    process_all(tmp_path, bot, queue, keep=False)
    assert not job_directory(queue, queue.state.jobs[0]).exists()
    assert queue_at(tmp_path).next_job() is None
    assert not queue_at(tmp_path).enqueue(message)


def test_truncated_photo_is_never_uploaded(tmp_path: Path) -> None:
    message = forwarded(1, kind="photo")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.payload = b"short"
    with pytest.raises(AppError, match="incomplete download"):
        process_all(tmp_path, bot, queue)
    assert not bot.delivered


def test_tampered_download_is_not_reused_after_uncertain_send(tmp_path: Path) -> None:
    message = forwarded(1, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.fail_final = 1
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    path = job_directory(queue, queue.state.jobs[0]) / "1" / "media.bin"
    path.write_bytes(b"x" * len(bot.payload))
    restarted = FakeBot([message])
    with pytest.raises(AppError, match="downloaded file changed"):
        process_all(tmp_path, restarted, queue_at(tmp_path))
    assert not restarted.delivered


def test_confirmation_save_failure_restarts_with_identical_album_ids(
    tmp_path: Path, monkeypatch
) -> None:
    messages = [forwarded(1, kind="photo", group=2), forwarded(2, kind="document", group=2)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)
    bot = FakeBot(messages)

    def fail_confirmation(*args, **kwargs):
        raise AppError("cannot save confirmation")

    monkeypatch.setattr(queue, "confirm", fail_confirmation)
    with pytest.raises(AppError, match="cannot save confirmation"):
        process_all(tmp_path, bot, queue)
    restored = queue_at(tmp_path)
    assert restored.state.jobs[0].status == "sending"
    resumed = FakeBot([])
    resumed.delivered = bot.delivered
    process_all(tmp_path, resumed, restored)
    assert len(resumed.delivered) == 2
    assert not any(kind == "download" for kind, _ in resumed.timeline)
    assert restored.state.jobs[0].output_ids == [10000, 10001]


def test_async_fingerprint_yields_to_updates_and_detects_changes(tmp_path: Path) -> None:
    from tg_comment_uploader.local_files import CHUNK_SIZE, fingerprint_file_async

    path = tmp_path / "video.bin"
    path.write_bytes(b"a" * (CHUNK_SIZE * 2))

    async def scenario() -> None:
        task = asyncio.create_task(fingerprint_file_async(path))
        await asyncio.sleep(0)
        assert not task.done()
        path.write_bytes(b"b" * CHUNK_SIZE)
        with pytest.raises(AppError, match="changed while fingerprinting"):
            await task

    asyncio.run(scenario())


def test_private_chat_downloads_and_returns_backup_to_the_same_user(tmp_path: Path) -> None:
    message = forwarded(1, kind="photo")
    message.peer_id = types.PeerUser(42)
    message.from_id = types.PeerUser(42)
    message.post = False
    bot = FakeBot([message])
    bot.peer = types.PeerUser(42)
    bot.input_peer = types.InputPeerUser(42, 123)
    queue = ReuploadQueue(
        tmp_path / "state.json",
        owner="private-test",
        chat_id=42,
        download_root=tmp_path / "downloads",
    )
    queue.enqueue(message)
    process_all(tmp_path, bot, queue)
    request = next(r for r in bot.requests if isinstance(r, functions.messages.SendMediaRequest))
    assert request.peer == bot.input_peer
    assert bot.sent_payloads == [bot.payload]
    assert queue.next_job() is None


@pytest.mark.parametrize("kind", ["text", "photo", "document"])
def test_formatted_date_metadata_resumes_and_allows_later_messages(
    tmp_path: Path, kind: str
) -> None:
    from reupload_fakes import NOW

    message = forwarded(1, kind=kind)
    entity = types.MessageEntityFormattedDate(0, 2, NOW, relative=True, long_date=True)
    message.entities = [entity]
    following = forwarded(2, text="next message")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(following)
    job = queue.next_job()
    assert job is not None
    queue.begin(job)  # The previous process failed while writing message metadata.
    restored = queue_at(tmp_path)
    bot = FakeBot([message, following])
    process_all(tmp_path, bot, restored)

    metadata_path = job_directory(restored, restored.state.jobs[0]) / "1" / "message.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["entities"][0]["date"] == NOW.isoformat()
    assert metadata["entities"][0]["relative"] is True
    assert metadata["entities"][0]["long_date"] is True
    request = next(
        r
        for r in bot.requests
        if isinstance(
            r, (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest)
        )
    )
    assert bytes(request.entities[0]) == bytes(entity)
    assert entity.date == NOW  # Metadata serialization did not mutate the upload entity.
    assert bot.sent_text == [message.message, following.message]
    assert queue_at(tmp_path).next_job() is None
