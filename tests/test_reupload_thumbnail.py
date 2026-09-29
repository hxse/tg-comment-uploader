from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
from telethon import errors, functions, types

from reupload_fakes import THUMBNAIL_JPEG, FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import job_directory


def video(message_id=1, *, group=None):
    message = forwarded(message_id, kind="document", group=group)
    assert isinstance(message.media, types.MessageMediaDocument)
    assert isinstance(message.media.document, types.Document)
    message.media.document.thumbs = [types.PhotoSize("m", 16, 16, len(THUMBNAIL_JPEG))]
    return message


def uploaded_media(bot):
    return [
        request.media
        for request in bot.requests
        if isinstance(
            request, (functions.messages.SendMediaRequest, functions.messages.UploadMediaRequest)
        )
        and isinstance(request.media, types.InputMediaUploadedDocument)
    ]


@pytest.mark.parametrize("album", [False, True])
def test_original_thumbnail_bytes_are_uploaded_for_single_video_and_album(
    tmp_path: Path, monkeypatch, album: bool
) -> None:
    messages = [video(i, group=7 if album else None) for i in range(1, 3 if album else 2)]
    queue = queue_at(tmp_path)
    for message in messages:
        queue.enqueue(message)

    def forbidden(*args, **kwargs):
        raise AssertionError("usable original thumbnail must not invoke FFmpeg")

    monkeypatch.setattr("tg_comment_uploader.thumbnail_image.subprocess.Popen", forbidden)
    bot = FakeBot(messages)
    process_all(tmp_path, bot, queue)
    assert bot.thumbnail_downloads == [m.id for m in messages]
    assert bot.sent_payloads == [bot.payload] * len(messages)
    media = uploaded_media(bot)
    assert len(media) == len(messages)
    assert len({m.thumb.id for m in media}) == len(messages)
    for original, uploaded, saved in zip(
        messages, media, queue.state.jobs[0].messages, strict=True
    ):
        assert uploaded.thumb is not None and uploaded.thumb.id != uploaded.file.id
        assert bot.file_bytes(uploaded.thumb) == THUMBNAIL_JPEG
        assert [bytes(a) for a in uploaded.attributes] == [
            bytes(a) for a in original.media.document.attributes
        ]
        assert saved.thumbnail is not None and saved.thumbnail.source == "original"
        assert saved.thumbnail.sha256 == hashlib.sha256(THUMBNAIL_JPEG).hexdigest()
        assert (
            job_directory(queue, queue.state.jobs[0]) / str(original.id) / "thumbnail.jpg"
        ).read_bytes() == THUMBNAIL_JPEG


def test_interrupted_send_reuses_cached_thumbnail_even_after_source_is_deleted(
    tmp_path: Path,
) -> None:
    message = video()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.fail_final = 1
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    saved = queue_at(tmp_path)
    assert saved.state.jobs[0].status == "sending"
    resumed = FakeBot([])
    resumed.delivered = dict(bot.delivered)
    process_all(tmp_path, resumed, saved)
    assert not resumed.thumbnail_downloads
    assert not resumed.message_lookups
    assert not any(kind == "download" for kind, _ in resumed.timeline)
    assert len(resumed.delivered) == 1
    assert bot.file_bytes(uploaded_media(bot)[0].thumb) == resumed.file_bytes(
        uploaded_media(resumed)[0].thumb
    )


def test_expired_thumbnail_reference_refreshes_locator_without_changing_caption(
    tmp_path: Path,
) -> None:
    original = video()
    fresh = copy.deepcopy(original)
    fresh.media.document.file_reference = b"new-thumbnail-reference"
    fresh.message = "edited caption must not replace snapshot"
    queue = queue_at(tmp_path)
    queue.enqueue(original)
    snapshot = queue.state.jobs[0].messages[0].snapshot
    bot = FakeBot([fresh])

    async def need_fresh(message):
        if message.media.document.file_reference != b"new-thumbnail-reference":
            raise errors.FileReferenceExpiredError(request=None)

    bot.thumbnail_hook = need_fresh
    process_all(tmp_path, bot, queue)
    assert bot.thumbnail_downloads == [1, 1]
    assert bot.message_lookups == [1]
    assert bot.sent_text == [original.message]
    assert queue.state.jobs[0].messages[0].snapshot == snapshot


def test_temporary_thumbnail_failure_retains_video_and_retries_without_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    message = video()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])

    async def fail(message):
        raise ConnectionError("temporary thumbnail download failure")

    async def forbidden(*args, **kwargs):
        raise AssertionError("temporary network failure must not replace the original thumbnail")

    bot.thumbnail_hook = fail
    monkeypatch.setattr("tg_comment_uploader.reupload_thumbnail.video_thumbnail", forbidden)
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    saved = queue_at(tmp_path)
    item = saved.state.jobs[0].messages[0]
    assert item.downloaded is not None and item.thumbnail is None
    assert not bot.delivered
    resumed = FakeBot([])
    process_all(tmp_path, resumed, saved)
    assert not any(kind == "download" for kind, _ in resumed.timeline)
    assert resumed.thumbnail_downloads == [1]


def test_deleted_original_thumbnail_falls_back_to_verified_local_video(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    message = video()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([])
    generated = []

    async def expired(message):
        raise errors.FileReferenceExpiredError(request=None)

    async def generate(path, **kwargs):
        generated.append(path)
        assert path.read_bytes() == bot.payload
        return THUMBNAIL_JPEG

    bot.thumbnail_hook = expired
    monkeypatch.setattr("tg_comment_uploader.reupload_thumbnail.video_thumbnail", generate)
    process_all(tmp_path, bot, queue)
    assert len(generated) == 1 and bot.message_lookups == [1]
    record = queue.state.jobs[0].messages[0].thumbnail
    assert record is not None and record.source == "generated"
    assert bot.sent_payloads == [bot.payload]
    assert "original thumbnail unavailable" in capsys.readouterr().out


def test_tampered_cached_thumbnail_is_never_sent_or_silently_replaced(tmp_path: Path) -> None:
    message = video()
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.fail_final = 1
    with pytest.raises(AppError):
        process_all(tmp_path, bot, queue)
    path = job_directory(queue, queue.state.jobs[0]) / "1" / "thumbnail.jpg"
    changed = bytearray(path.read_bytes())
    changed[changed.index(b"Lavc") + 4] ^= 1
    path.write_bytes(changed)
    resumed = FakeBot([])
    with pytest.raises(AppError, match="cached thumbnail changed"):
        process_all(tmp_path, resumed, queue_at(tmp_path))
    assert not resumed.requests and not resumed.thumbnail_downloads


def test_incomplete_thumbnail_blocks_upload_and_later_tasks(tmp_path: Path) -> None:
    message = video()
    message.media.document.thumbs[0].size += 1
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(forwarded(2))
    bot = FakeBot([message])
    with pytest.raises(AppError, match="thumbnail download is incomplete"):
        process_all(tmp_path, bot, queue)
    assert not bot.delivered
    assert queue.state.jobs[1].status == "queued"


def test_thumbnail_is_deleted_only_after_confirmed_upload(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(video())
    bot = FakeBot()
    process_all(tmp_path, bot, queue, keep=False)
    job = queue_at(tmp_path).state.jobs[0]
    assert job.status == "confirmed" and not job.cleanup_pending
    assert job.messages[0].thumbnail is not None
    assert not job_directory(queue, job).exists()


def test_non_video_document_without_thumbnail_needs_no_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    message = video()
    message.media.document.thumbs = []
    message.media.document.attributes = [types.DocumentAttributeFilename("example.txt")]
    message.media.document.mime_type = "text/plain"
    queue = queue_at(tmp_path)
    queue.enqueue(message)

    async def forbidden(*args, **kwargs):
        raise AssertionError("not a video")

    monkeypatch.setattr("tg_comment_uploader.reupload_thumbnail.video_thumbnail", forbidden)
    bot = FakeBot([message])
    process_all(tmp_path, bot, queue)
    assert uploaded_media(bot)[0].thumb is None
