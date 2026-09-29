from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest
from telethon import types

from reupload_fakes import THUMBNAIL_JPEG, FakeBot, forwarded, queue_at, sender_at
from test_reupload_delivery import process_all
from test_reupload_thumbnail import uploaded_media
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.telegram_sender import UploadItem, UploadThumbnail
from tg_comment_uploader.thumbnail_image import (
    MAX_THUMBNAIL_BYTES,
    jpeg_dimensions,
    normalize_thumbnail,
    read_thumbnail,
    validate_thumbnail,
    video_thumbnail,
)


@pytest.fixture
def real_video(tmp_path: Path) -> Path:
    path = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:r=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_real_ffmpeg_fallback_uploads_a_thumbnail_without_changing_video_bytes(
    tmp_path: Path, real_video: Path
) -> None:
    payload = real_video.read_bytes()
    message = forwarded(1, kind="document", payload=payload)
    assert isinstance(message.media, types.MessageMediaDocument)
    assert isinstance(message.media.document, types.Document)
    message.media.document.thumbs = []
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    bot = FakeBot([message])
    bot.payload = payload
    before = hashlib.sha256(payload).hexdigest()
    process_all(tmp_path, bot, queue)
    media = uploaded_media(bot)[0]
    jpeg = bot.file_bytes(media.thumb)
    assert jpeg_dimensions(jpeg) == (320, 180)
    assert len(jpeg) <= MAX_THUMBNAIL_BYTES
    assert bot.sent_payloads == [payload]
    assert hashlib.sha256(real_video.read_bytes()).hexdigest() == before
    saved = queue.state.jobs[0].messages[0]
    assert saved.thumbnail is not None and saved.thumbnail.source == "generated"
    assert saved.downloaded is not None and saved.downloaded.sha256 == before


def test_oversized_original_jpeg_is_scaled_without_touching_usable_originals() -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=640x360",
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    normalized = asyncio.run(normalize_thumbnail(result.stdout))
    assert jpeg_dimensions(normalized) == (320, 180)
    assert len(normalized) <= MAX_THUMBNAIL_BYTES
    assert asyncio.run(normalize_thumbnail(THUMBNAIL_JPEG)) == THUMBNAIL_JPEG


def test_shared_local_upload_sender_accepts_a_verified_thumbnail(
    tmp_path: Path, real_video: Path
) -> None:
    path = tmp_path / "thumbnail.jpg"
    path.write_bytes(THUMBNAIL_JPEG)
    thumbnail = UploadThumbnail(
        path, len(THUMBNAIL_JPEG), hashlib.sha256(THUMBNAIL_JPEG).hexdigest()
    )
    item = UploadItem(
        real_video,
        "caption",
        real_video.stat().st_size,
        hashlib.sha256(real_video.read_bytes()).hexdigest(),
        thumbnail=thumbnail,
    )
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:
        sender.send_video(item, random_id=101)
    assert bot.file_bytes(uploaded_media(bot)[0].thumb) == THUMBNAIL_JPEG
    assert bot.sent_payloads == [real_video.read_bytes()]


def test_shared_sender_checks_thumbnail_hash_before_final_send(
    tmp_path: Path, real_video: Path
) -> None:
    path = tmp_path / "thumbnail.jpg"
    changed = bytearray(THUMBNAIL_JPEG)
    changed[changed.index(b"Lavc") + 4] ^= 1
    path.write_bytes(changed)
    thumbnail = UploadThumbnail(
        path, len(THUMBNAIL_JPEG), hashlib.sha256(THUMBNAIL_JPEG).hexdigest()
    )
    item = UploadItem(
        real_video,
        "caption",
        real_video.stat().st_size,
        hashlib.sha256(real_video.read_bytes()).hexdigest(),
        thumbnail=thumbnail,
    )
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:
        with pytest.raises(AppError, match="no longer matches its preflight identity"):
            sender.send_video(item, random_id=101)
    assert not bot.delivered


@pytest.mark.parametrize("payload", [b"not a jpeg", THUMBNAIL_JPEG[:-2], b"\xff\xd8\xff\xd9"])
def test_invalid_jpeg_is_not_uploadable(payload: bytes) -> None:
    with pytest.raises(AppError, match="JPEG"):
        validate_thumbnail(payload)


def test_thumbnail_reader_rejects_symlinks(tmp_path: Path) -> None:
    original = tmp_path / "original.jpg"
    original.write_bytes(THUMBNAIL_JPEG)
    link = tmp_path / "thumbnail.jpg"
    link.symlink_to(original)
    with pytest.raises(OSError):
        read_thumbnail(link)


def test_cancelling_ffmpeg_generation_terminates_and_reaps_the_child(monkeypatch) -> None:
    async def scenario():
        entered = asyncio.Event()

        class Child:
            returncode = None
            reaped = False

            def poll(self):
                entered.set()
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                self.reaped = True
                return self.returncode

        child = Child()

        def spawn(*args, **kwargs):
            return child

        monkeypatch.setattr("tg_comment_uploader.thumbnail_image.subprocess.Popen", spawn)
        task = asyncio.create_task(video_thumbnail(Path("unused.mp4"), duration=1))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.returncode == -15 and child.reaped

    asyncio.run(scenario())
