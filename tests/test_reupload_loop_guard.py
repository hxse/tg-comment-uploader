from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import functions, types

from reupload_fakes import BOT, FakeBot, forwarded, queue_at
from test_reupload_delivery import process_all
from tg_comment_uploader.reupload_service import ReuploadService


@pytest.mark.parametrize("out", [False, True])
@pytest.mark.parametrize("sender", [None, types.PeerChannel(123), types.PeerUser(BOT)])
def test_own_channel_updates_before_confirmation_and_after_restart_never_reenter_queue(
    tmp_path: Path, sender, out: bool
) -> None:
    queue = queue_at(tmp_path)
    messages = [
        forwarded(1),
        forwarded(2, kind="photo", group=7),
        forwarded(3, kind="document", group=7),
    ]
    for message in messages:
        queue.enqueue(message)
    echoes = []

    class ChannelEchoBot(FakeBot):
        async def __call__(self, request):
            if not isinstance(
                request,
                (
                    functions.messages.SendMessageRequest,
                    functions.messages.SendMediaRequest,
                    functions.messages.SendMultiMediaRequest,
                ),
            ):
                return await super().__call__(request)
            receive = self.handler

            async def echo(event):
                event.message.from_id = sender
                event.message.out = out
                echoes.append(event.message)
                await receive(event)

            self.handler = echo
            try:
                return await super().__call__(request)
            finally:
                self.handler = receive

    bot = ChannelEchoBot(messages)
    process_all(tmp_path, bot, queue)
    assert len(bot.delivered) == 3 and len(queue.state.jobs) == 2
    assert all(job.status == "confirmed" for job in queue.state.jobs)
    restored = queue_at(tmp_path)
    before = restored.path.read_bytes()
    service = ReuploadService(restored, bot_id=BOT, retries=0)

    async def replay():
        for _ in range(3):
            for message in echoes:
                await service.on_message(SimpleNamespace(message=message))

    asyncio.run(replay())
    service.check_intake()
    assert restored.next_job() is None
    assert restored.path.read_bytes() == before
