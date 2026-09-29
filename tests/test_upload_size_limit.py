from __future__ import annotations

from pathlib import Path

import pytest
from telethon import functions, types

from reupload_fakes import FakeBot, forwarded, queue_at
from test_mtproto_sender import FakeClient, make_sender
from test_reupload_delivery import process_all
from tg_comment_uploader.cli import prepared_media_target_bytes, validate_upload_paths
from tg_comment_uploader.errors import AppError, RetryableUploadError
from tg_comment_uploader.media_workflow import prepare_media
from tg_comment_uploader.reupload_message import decode_message, make_upload_item
from tg_comment_uploader.telegram_sender import SAFE_UPLOAD_LIMIT_BYTES


@pytest.mark.parametrize(
    ("size", "expected_parts"),
    [(2_000_000_001, 3815), (2_071_997_095, 3953), (2_097_152_000, 4000)],
)
def test_old_limit_blocked_snapshot_and_shared_sender_accept_larger_files(
    tmp_path: Path, size: int, expected_parts: int
) -> None:
    message = forwarded(94, kind="document")
    assert isinstance(message.media, types.MessageMediaDocument)
    assert isinstance(message.media.document, types.Document)
    message.media.document.size = size
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    job = queue.next_job()
    assert job is not None
    queue.begin(job)  # State left behind by the previous size check.
    saved = queue_at(tmp_path).next_job()
    assert saved is not None
    assert saved.random_ids == job.random_ids

    # A sparse file exercises size checks without allocating or hashing 2 GiB.
    path = tmp_path / "media.bin"
    with path.open("wb") as output:
        output.truncate(size)
    validate_upload_paths([str(path)])
    for policy in ("error", "split", "compress"):
        with prepare_media(
            path,
            policy,
            expected_source_size=size,
            expected_source_sha256="0" * 64,
            hard_limit_bytes=SAFE_UPLOAD_LIMIT_BYTES,
            target_bytes=prepared_media_target_bytes(policy),
        ) as prepared:
            assert prepared.paths == (path,)
    assert not (tmp_path / ".tg-comment-uploader-work").exists()

    item = make_upload_item(decode_message(saved.messages[0].snapshot), path, size, "0" * 64)
    client = FakeClient(failure=("upload_file", ConnectionResetError("test interruption")))
    with make_sender(tmp_path, client=client) as sender:
        with pytest.raises(RetryableUploadError):
            sender.send_video(item, random_id=saved.random_ids[0])
    parts = [r for r in client.requests if isinstance(r, functions.upload.SaveBigFilePartRequest)]
    assert parts
    assert all(r.file_total_parts == expected_parts for r in parts)
    assert all(len(r.bytes) == 512 * 1024 for r in parts)
    assert not any(isinstance(r, functions.messages.SendMediaRequest) for r in client.requests)


@pytest.mark.parametrize("size", [0, 2_097_152_001, 2_147_483_648])
def test_reupload_rejects_invalid_sizes_before_download_and_reports_actual_size(
    tmp_path: Path, size: int
) -> None:
    message = forwarded(94, kind="document")
    assert isinstance(message.media, types.MessageMediaDocument)
    assert isinstance(message.media.document, types.Document)
    message.media.document.size = size
    following = forwarded(95)
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(following)
    bot = FakeBot([message, following])

    with pytest.raises(AppError, match="this project's allowed range") as caught:
        process_all(tmp_path, bot, queue)
    assert f"size={size:,} bytes" in str(caught.value)
    assert "1..2,097,152,000 bytes" in str(caught.value)
    assert not bot.timeline
    assert not bot.requests
    assert queue.state.jobs[1].status == "queued"
