from __future__ import annotations

import json
from pathlib import Path

import pytest
from telethon import functions, types

from reupload_fakes import NOW, FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import job_directory
from tg_comment_uploader.reupload_message import message_kind

URL = "https://example.com/watch"


@pytest.mark.parametrize("download_only", [False, True])
@pytest.mark.parametrize("preview_url", [None, URL])
def test_empty_preview_resumes_saved_task_as_text_and_allows_following_video(
    tmp_path: Path, download_only: bool, preview_url: str | None
) -> None:
    prefix = "说明 😀 "
    message = forwarded(289, text=prefix + URL)
    message.entities = [
        types.MessageEntityBold(0, 2),
        types.MessageEntityUrl(len(prefix.encode("utf-16-le")) // 2, len(URL)),
    ]
    message.media = types.MessageMediaWebPage(types.WebPageEmpty(123, url=preview_url))
    following = forwarded(290, kind="document")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(following)
    job = queue.state.jobs[0]
    queue.begin(job)  # The previous version stopped here on MessageMediaWebPage.
    snapshot = job.messages[0].snapshot
    random_ids = [saved.random_ids for saved in queue.state.jobs]

    restored = queue_at(tmp_path)
    bot = FakeBot([])
    process_all(tmp_path, bot, restored, download_only=download_only)

    saved = queue_at(tmp_path)
    assert all(j.status == ("ready" if download_only else "confirmed") for j in saved.state.jobs)
    assert saved.state.jobs[0].messages[0].snapshot == snapshot
    assert [j.random_ids for j in saved.state.jobs] == random_ids
    assert bot.timeline[0][0] == ("download" if download_only else "send")
    assert [value for stage, value in bot.timeline if stage == "download"] == [290]
    assert not bot.message_lookups
    metadata = json.loads(
        (job_directory(saved, saved.state.jobs[0]) / "289/message.json").read_text()
    )
    assert metadata["kind"] == "text"
    assert metadata["text"] == message.message
    assert metadata["entities"] == [entity.to_dict() for entity in message.entities]
    if download_only:
        assert not bot.requests and not bot.parts and not bot.delivered
    else:
        request = next(
            r for r in bot.requests if isinstance(r, functions.messages.SendMessageRequest)
        )
        assert request.message == message.message
        assert [bytes(entity) for entity in request.entities] == [
            bytes(entity) for entity in message.entities
        ]
        assert request.no_webpage is True
        assert bot.sent_text == [message.message, following.message]
        assert bot.sent_payloads == [bot.payload]

    restarted = FakeBot([])
    process_all(tmp_path, restarted, saved, download_only=download_only)
    assert not restarted.requests and not restarted.timeline


@pytest.mark.parametrize(
    "preview",
    [
        types.WebPagePending(123, NOW, url=URL),
        types.WebPage(123, URL, "example.com/watch", 0, title="Preview content"),
    ],
    ids=["pending", "populated"],
)
def test_nonempty_or_pending_previews_are_not_silently_dropped(tmp_path: Path, preview) -> None:
    message = forwarded(289, text=URL)
    message.media = types.MessageMediaWebPage(preview)
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(forwarded(290, kind="document"))
    bot = FakeBot([])
    with pytest.raises(AppError, match="MessageMediaWebPage is not supported"):
        process_all(tmp_path, bot, queue)
    assert not bot.requests and not bot.timeline
    assert queue_at(tmp_path).state.jobs[1].status == "queued"


def test_empty_preview_without_text_remains_an_error() -> None:
    message = forwarded(289, text="")
    message.media = types.MessageMediaWebPage(types.WebPageEmpty(123))
    with pytest.raises(AppError, match="empty content"):
        message_kind(message)
