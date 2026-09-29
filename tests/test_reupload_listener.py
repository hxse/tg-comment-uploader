from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, CHAT, FakeBot, forwarded, queue_at, sender_at
from tg_comment_uploader.errors import AppError, RetryableUploadError
from tg_comment_uploader.reupload_queue import ReuploadQueue
from tg_comment_uploader.reupload_service import ReuploadService


async def until_confirmed(queue: ReuploadQueue, count: int) -> None:
    async def wait() -> None:
        while sum(len(j.output_ids) for j in queue.state.jobs) != count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=3)


def test_listener_accepts_more_inputs_while_download_is_suspended(tmp_path: Path) -> None:
    first, second = forwarded(1, kind="document"), forwarded(2)
    queue = queue_at(tmp_path)
    bot = FakeBot([first])
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True, settle_seconds=0)
    bot.handler = service.on_message
    with sender_at(tmp_path, bot) as sender:

        async def scenario() -> None:
            entered, release = asyncio.Event(), asyncio.Event()

            async def blocked_download(message):
                entered.set()
                await release.wait()

            bot.download_hook = blocked_download
            listener = asyncio.create_task(service.run(sender))
            try:
                await service.on_message(SimpleNamespace(message=first))
                await asyncio.wait_for(entered.wait(), 3)
                await service.on_message(SimpleNamespace(message=second))
                assert len(queue_at(tmp_path).state.jobs) == 2
                assert not bot.delivered
                release.set()
                await until_confirmed(queue, 2)
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())
    assert bot.sent_text == [first.message, second.message]


def test_partial_album_waits_and_later_messages_do_not_overtake(tmp_path: Path) -> None:
    messages = [
        forwarded(1, kind="photo", group=7),
        forwarded(2, kind="document", group=7),
        forwarded(3),
    ]
    queue = queue_at(tmp_path)
    bot = FakeBot(messages)
    service = ReuploadService(
        queue, bot_id=BOT, retries=0, keep_downloads=True, settle_seconds=0.01
    )
    with sender_at(tmp_path, bot) as sender:

        async def scenario() -> None:
            listener = asyncio.create_task(service.run(sender))
            try:
                await service.on_message(SimpleNamespace(message=messages[0]))
                await service.on_message(SimpleNamespace(message=messages[2]))
                await asyncio.sleep(0.25)
                assert not bot.delivered
                await service.on_message(SimpleNamespace(message=messages[1]))
                await until_confirmed(queue, 3)
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())
    assert bot.sent_payloads == [bot.payload] * 2
    assert [j.first_id for j in queue.state.jobs] == [1, 3]


def test_intake_write_error_is_propagated_out_of_telethon_handler(
    tmp_path: Path, monkeypatch
) -> None:
    queue = queue_at(tmp_path)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)

    def broken(*args, **kwargs):
        raise OSError("sensitive-error-detail")

    monkeypatch.setattr(queue, "enqueue", broken)
    asyncio.run(service.on_message(SimpleNamespace(message=forwarded(1))))
    with pytest.raises(AppError, match="could not queue") as caught:
        service.check_intake()
    assert "sensitive-error-detail" not in str(caught.value)


def test_disconnect_preserves_queue_and_exits_for_reconnect(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    bot = FakeBot([])
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)
    with sender_at(tmp_path, bot) as sender:

        async def scenario() -> None:
            task = asyncio.create_task(service.run(sender))
            await asyncio.sleep(0)
            await bot.disconnect()
            await task

        with pytest.raises(RetryableUploadError, match="disconnected"):
            sender.run_service(scenario())
    pending = queue_at(tmp_path).next_job()
    assert pending is not None and pending.status == "queued"


def test_client_factory_registers_before_login_with_sequential_updates(
    tmp_path: Path, monkeypatch
) -> None:
    queue = queue_at(tmp_path)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)

    class LoginBot(FakeBot):
        def __init__(self, *args, **kwargs):
            super().__init__()
            assert kwargs["sequential_updates"] is True

        def add_event_handler(self, handler, builder):
            self.handler = handler

        async def start(self, *, bot_token):
            assert self.handler is not None
            await self.handler(SimpleNamespace(message=forwarded(1)))

    monkeypatch.setattr("tg_comment_uploader.reupload_service.TelegramClient", LoginBot)
    bot = service.client_factory("session", 1, "fake")
    with sender_at(tmp_path, bot) as sender:
        sender.run_service(sender.connect())
    pending = queue_at(tmp_path).next_job()
    assert pending is not None and pending.first_id == 1


def test_wrong_resolved_peer_stops_before_any_send(tmp_path: Path) -> None:
    from telethon import types

    class WrongPeer(FakeBot):
        async def get_input_entity(self, chat_id):
            assert chat_id == CHAT
            return types.InputPeerChannel(999, 123)

    queue = queue_at(tmp_path)
    bot = WrongPeer([])
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)
    with sender_at(tmp_path, bot) as sender:
        with pytest.raises(AppError, match="does not match"):
            sender.run_service(service.run(sender))
    assert not bot.requests


@pytest.mark.parametrize("new_messages_since", [None, 1_700_000_000])
def test_reconnect_catches_up_from_saved_session_progress(
    tmp_path: Path,
    monkeypatch,
    new_messages_since: int | None,
) -> None:
    from unittest.mock import AsyncMock

    from telethon import functions, types
    from telethon._updates.messagebox import GapError
    from telethon.sessions import SQLiteSession

    from reupload_fakes import NOW

    session_path = str(tmp_path / "saved.session")
    session = SQLiteSession(session_path)
    session.set_update_state(0, types.updates.State(100, 0, NOW, 1, 0))
    session.set_update_state(123, types.updates.State(80, 0, NOW, 0, 0))
    session.process_entities(
        [types.Channel(123, "backup", types.ChatPhotoEmpty(), NOW, access_hash=456, broadcast=True)]
    )
    session.save()
    session.close()
    service = ReuploadService(
        queue_at(tmp_path),
        bot_id=BOT,
        retries=0,
        keep_downloads=True,
        new_messages_since=new_messages_since,
    )

    async def scenario() -> None:
        # Exercise Telethon's actual session restoration and message box;
        # replace only transport calls and the two background loops.
        client = service.client_factory(session_path, 123, "fake-api-hash")
        monkeypatch.setattr(client._sender, "connect", AsyncMock(return_value=True))
        monkeypatch.setattr(client._sender, "send", AsyncMock())
        monkeypatch.setattr(client._sender, "disconnect", AsyncMock())
        monkeypatch.setattr(client, "_update_loop", AsyncMock())
        monkeypatch.setattr(client, "_keepalive_loop", AsyncMock())
        monkeypatch.setattr(client, "get_me", AsyncMock(return_value=types.User(BOT, bot=True)))

        async def server_call(sender, request, **kwargs):
            if isinstance(request, functions.updates.GetStateRequest):
                return types.updates.State(150, 0, NOW, 2, 0)
            if isinstance(request, functions.updates.GetDifferenceRequest):
                return types.updates.DifferenceEmpty(NOW, 2)
            raise AssertionError(f"unexpected RPC: {type(request).__name__}")

        monkeypatch.setattr(client, "_call", server_call)
        try:
            await client.connect()
            await client.catch_up()
            update = client._updates_queue.get_nowait()
            with pytest.raises(GapError):
                client._message_box.process_updates(update, client._mb_entity_cache, [])
            difference = client._message_box.get_difference()
            assert difference is not None and difference.pts == 100
            assert client._message_box.session_state()[1] == {123: 80}
            client._message_box.try_begin_get_diff(123, "test channel reconnect")
            channel_difference = client._message_box.get_channel_difference(client._mb_entity_cache)
            assert channel_difference is not None and channel_difference.pts == 80
        finally:
            await client.disconnect()

    asyncio.run(scenario())
