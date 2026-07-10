from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.cli import (
    NonRetryableUploadError,
    RetryableUploadError,
    post_json,
    post_multipart,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str) -> str | None:
        return None


def test_post_json_sends_compact_utf8_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []
    headers: dict[str, str] = {}

    class FakeConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, method: str, path: str) -> None:
            assert method == "POST"
            assert path == "/bottoken/sendMediaGroup"

        def putheader(self, name: str, value: str) -> None:
            headers[name] = value

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            sent.append(body)

        def getresponse(self) -> FakeResponse:
            return FakeResponse(200, {"ok": True, "result": []})

        def close(self) -> None:
            pass

    monkeypatch.setattr("tg_comment_uploader.cli.http.client.HTTPConnection", FakeConnection)

    payload = {"chat_id": "-1001", "caption": "中文"}
    result = post_json(
        host="127.0.0.1",
        port=48973,
        token="token",
        method="sendMediaGroup",
        payload=payload,
        file_path=tmp_path / "part.mp4",
    )

    assert result == {"ok": True, "result": []}
    assert json.loads(sent[0].decode()) == payload
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Content-Length"] == str(len(sent[0]))


def test_post_json_does_not_retry_when_response_is_lost_after_body_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LostResponseConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            pass

        def putheader(self, *args: Any) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            raise TimeoutError("lost response")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        LostResponseConnection,
    )

    with pytest.raises(NonRetryableUploadError, match="outcome is unknown"):
        post_json(
            host="127.0.0.1",
            port=48973,
            token="token",
            method="sendMediaGroup",
            payload={"chat_id": "-1001", "media": []},
            file_path=tmp_path / "part.mp4",
        )


def test_post_json_classifies_known_rate_limit_as_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            pass

        def putheader(self, *args: Any) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse(
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 4},
                },
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        RateLimitedConnection,
    )

    with pytest.raises(RetryableUploadError) as exc_info:
        post_json(
            host="127.0.0.1",
            port=48973,
            token="token",
            method="sendMediaGroup",
            payload={"chat_id": "-1001", "media": []},
            file_path=tmp_path / "part.mp4",
        )

    assert exc_info.value.retry_after_seconds == 4


class RecordingWaitIndicator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> RecordingWaitIndicator:
        self.events.append("wait-start")
        return self

    def __exit__(self, *args: object) -> None:
        self.events.append("wait-stop")


def test_post_json_wait_indicator_covers_getresponse_and_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class EventResponse(FakeResponse):
        def read(self) -> bytes:
            events.append("read")
            return super().read()

    class EventConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            pass

        def putheader(self, *args: Any) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            events.append("body-sent")

        def getresponse(self) -> FakeResponse:
            events.append("getresponse")
            return EventResponse(200, {"ok": True, "result": []})

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("tg_comment_uploader.cli.http.client.HTTPConnection", EventConnection)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.ResponseWaitIndicator",
        lambda: RecordingWaitIndicator(events),
    )

    post_json(
        host="127.0.0.1",
        port=48973,
        token="token",
        method="sendMediaGroup",
        payload={"chat_id": "-1001", "media": []},
        file_path=tmp_path / "part.mp4",
    )

    assert events == [
        "body-sent",
        "wait-start",
        "getresponse",
        "read",
        "wait-stop",
        "close",
    ]


def test_post_multipart_wait_indicator_covers_getresponse_and_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    class EventResponse(FakeResponse):
        def read(self) -> bytes:
            events.append("read")
            return super().read()

    class EventConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            pass

        def putheader(self, *args: Any) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            events.append("send")

        def getresponse(self) -> FakeResponse:
            events.append("getresponse")
            return EventResponse(200, {"ok": True, "result": {"message_id": 1}})

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("tg_comment_uploader.cli.http.client.HTTPConnection", EventConnection)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.send_file_with_progress",
        lambda *args: events.append("file-body"),
    )
    monkeypatch.setattr(
        "tg_comment_uploader.cli.ResponseWaitIndicator",
        lambda: RecordingWaitIndicator(events),
    )

    result = post_multipart(
        host="127.0.0.1",
        port=48973,
        token="token",
        method="sendVideo",
        fields={"chat_id": "-1001"},
        file_field="video",
        file_path=video,
    )

    assert result == {"ok": True, "result": {"message_id": 1}}
    assert events[-5:] == ["wait-start", "getresponse", "read", "wait-stop", "close"]
    assert events.index("file-body") < events.index("wait-start")


def test_response_wait_context_exits_before_keyboard_interrupt_escapes_post_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class InterruptedConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            pass

        def putheader(self, *args: Any) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            events.append("body-sent")

        def getresponse(self) -> FakeResponse:
            events.append("getresponse")
            raise KeyboardInterrupt

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        InterruptedConnection,
    )
    monkeypatch.setattr(
        "tg_comment_uploader.cli.ResponseWaitIndicator",
        lambda: RecordingWaitIndicator(events),
    )

    with pytest.raises(KeyboardInterrupt):
        post_json(
            host="127.0.0.1",
            port=48973,
            token="token",
            method="sendMediaGroup",
            payload={"chat_id": "-1001", "media": []},
            file_path=tmp_path / "part.mp4",
        )

    assert events == ["body-sent", "wait-start", "getresponse", "wait-stop", "close"]
