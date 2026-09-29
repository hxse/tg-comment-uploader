from __future__ import annotations

from pathlib import Path

import pytest
from telethon import functions, types

from reupload_fakes import FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all


@pytest.mark.parametrize("kind", ["text", "photo", "document"])
@pytest.mark.parametrize(
    "markup",
    [
        types.ReplyInlineMarkup(
            [types.KeyboardButtonRow([types.KeyboardButtonUrl("打开", "https://example.com")])]
        ),
        types.ReplyInlineMarkup(
            [types.KeyboardButtonRow([types.KeyboardButtonCallback("操作", b"callback")])]
        ),
        types.ReplyInlineMarkup(
            [types.KeyboardButtonRow([types.KeyboardButtonSwitchInline("选择", "query")])]
        ),
        types.ReplyKeyboardMarkup(
            [
                types.KeyboardButtonRow(
                    [types.KeyboardButton("回复"), types.KeyboardButtonRequestPhone("电话")]
                )
            ]
        ),
        types.ReplyKeyboardForceReply(),
        types.ReplyKeyboardHide(),
    ],
    ids=["url", "callback", "switch-inline", "reply-keyboard", "force-reply", "hide-keyboard"],
)
def test_saved_task_with_markup_resumes_body_backup_and_allows_following_messages(
    tmp_path: Path, kind: str, markup: types.TypeReplyMarkup
) -> None:
    message = forwarded(85, kind=kind)
    message.reply_markup = markup
    following = forwarded(86, text="下一条消息")
    queue = queue_at(tmp_path)
    queue.enqueue(message)
    queue.enqueue(following)
    job = queue.next_job()
    assert job is not None
    queue.begin(job)  # Previous versions stopped here when validating reply markup.
    snapshot = job.messages[0].snapshot
    random_ids = [j.random_ids for j in queue.state.jobs]

    restored = queue_at(tmp_path)
    bot = FakeBot([message, following])
    process_all(tmp_path, bot, restored)

    requests = [
        r
        for r in bot.requests
        if isinstance(
            r, (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest)
        )
    ]
    assert len(requests) == 2
    assert all(r.reply_markup is None for r in requests)
    assert [bytes(e) for e in requests[0].entities] == [bytes(e) for e in message.entities or ()]
    assert bot.sent_text == [message.message, following.message]
    assert bot.sent_payloads == ([] if kind == "text" else [bot.payload])
    assert [value for stage, value in bot.timeline if stage == "download"] == (
        [] if kind == "text" else [85]
    )
    assert not bot.message_lookups
    saved = queue_at(tmp_path)
    assert saved.next_job() is None
    assert saved.state.jobs[0].messages[0].snapshot == snapshot
    assert [j.random_ids for j in saved.state.jobs] == random_ids
    finished = FakeBot([])
    process_all(tmp_path, finished, saved)
    assert not finished.requests


def test_album_with_buttons_downloads_all_media_and_preserves_order_without_buttons(
    tmp_path: Path,
) -> None:
    photo = forwarded(1, kind="photo", group=7, text="图片 😀")
    video = forwarded(2, kind="document", group=7, text="视频 😀")
    photo.reply_markup = types.ReplyInlineMarkup(
        [types.KeyboardButtonRow([types.KeyboardButtonCallback("操作", b"callback")])]
    )
    video.reply_markup = types.ReplyInlineMarkup(
        [types.KeyboardButtonRow([types.KeyboardButtonUrl("打开", "https://example.com")])]
    )
    queue = queue_at(tmp_path)
    queue.enqueue(video)
    queue.enqueue(photo)
    bot = FakeBot([photo, video])
    process_all(tmp_path, bot, queue)

    request = next(
        r for r in bot.requests if isinstance(r, functions.messages.SendMultiMediaRequest)
    )
    assert getattr(request, "reply_markup", None) is None
    assert [stage for stage, _ in bot.timeline] == ["download", "download", "send"]
    assert bot.sent_payloads == [bot.payload, bot.payload]
    assert bot.sent_text == [photo.message, video.message]
    for uploaded, original in zip(request.multi_media, (photo, video), strict=True):
        assert [bytes(e) for e in uploaded.entities or ()] == [
            bytes(e) for e in original.entities or ()
        ]
    assert len(queue_at(tmp_path).state.jobs) == 1
    assert queue_at(tmp_path).next_job() is None
