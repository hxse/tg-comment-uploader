from __future__ import annotations

import pytest
from telethon import types, utils
from typing import Any

from tg_comment_uploader.mtproto_sender import parse_send_result


def make_message(message_id: int, *, grouped_id: int | None) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(99),
        date=None,
        message="",
        grouped_id=grouped_id,
    )


def make_updates(*updates: Any) -> types.Updates:
    return types.Updates(
        updates=list(updates),
        users=[],
        chats=[],
        date=None,
        seq=1,
    )


def test_parse_single_send_result_uses_public_random_id_mapping() -> None:
    result = make_updates(
        types.UpdateMessageID(id=42, random_id=-123),
        types.UpdateNewChannelMessage(
            message=make_message(42, grouped_id=None),
            pts=1,
            pts_count=1,
        ),
    )

    assert parse_send_result(
        result,
        (-123,),
        expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
        expect_grouped=False,
    ) == (42,)


def test_parse_album_returns_input_random_id_order_not_update_order() -> None:
    result = make_updates(
        types.UpdateMessageID(id=102, random_id=22),
        types.UpdateNewChannelMessage(
            message=make_message(102, grouped_id=700),
            pts=1,
            pts_count=1,
        ),
        types.UpdateMessageID(id=101, random_id=11),
        types.UpdateNewChannelMessage(
            message=make_message(101, grouped_id=700),
            pts=2,
            pts_count=1,
        ),
    )

    assert parse_send_result(
        result,
        (11, 22),
        expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
        expect_grouped=True,
    ) == (101, 102)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            (
                types.UpdateNewChannelMessage(
                    message=make_message(1, grouped_id=None),
                    pts=1,
                    pts_count=1,
                ),
            ),
            "random-ID mapping",
        ),
        (
            (
                types.UpdateMessageID(id=1, random_id=123),
                types.UpdateNewChannelMessage(
                    message=make_message(2, grouped_id=None),
                    pts=1,
                    pts_count=1,
                ),
            ),
            "new-message count or identity",
        ),
    ],
)
def test_parse_send_result_rejects_incomplete_updates(
    updates: tuple[object, ...],
    message: str,
) -> None:
    with pytest.raises(Exception, match=message):
        parse_send_result(
            make_updates(*updates),
            (123,),
            expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
            expect_grouped=False,
        )


def test_parse_send_result_rejects_duplicate_identical_random_id_mapping() -> None:
    result = make_updates(
        types.UpdateMessageID(id=42, random_id=123),
        types.UpdateMessageID(id=42, random_id=123),
        types.UpdateNewChannelMessage(
            message=make_message(42, grouped_id=None),
            pts=1,
            pts_count=1,
        ),
    )

    with pytest.raises(Exception, match="duplicate message-ID mapping"):
        parse_send_result(
            result,
            (123,),
            expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
            expect_grouped=False,
        )


def test_parse_send_result_rejects_album_without_one_grouped_id() -> None:
    result = make_updates(
        types.UpdateMessageID(id=1, random_id=11),
        types.UpdateMessageID(id=2, random_id=22),
        types.UpdateNewChannelMessage(
            message=make_message(1, grouped_id=700),
            pts=1,
            pts_count=1,
        ),
        types.UpdateNewChannelMessage(
            message=make_message(2, grouped_id=701),
            pts=2,
            pts_count=1,
        ),
    )

    with pytest.raises(Exception, match="share one grouped_id"):
        parse_send_result(
            result,
            (11, 22),
            expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
            expect_grouped=True,
        )


def test_parse_single_accepts_official_update_short_sent_message() -> None:
    result = types.UpdateShortSentMessage(
        id=73,
        pts=1,
        pts_count=1,
        date=None,
        out=True,
    )

    assert parse_send_result(
        result,
        (123,),
        expected_peer_id=utils.get_peer_id(types.PeerChannel(99)),
        expect_grouped=False,
    ) == (73,)


def test_parse_send_result_rejects_message_for_wrong_peer() -> None:
    result = make_updates(
        types.UpdateMessageID(id=42, random_id=123),
        types.UpdateNewChannelMessage(
            message=make_message(42, grouped_id=None),
            pts=1,
            pts_count=1,
        ),
    )

    with pytest.raises(Exception, match="unexpected peer"):
        parse_send_result(
            result,
            (123,),
            expected_peer_id=utils.get_peer_id(types.PeerChannel(100)),
            expect_grouped=False,
        )
