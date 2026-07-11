from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.cli import (
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
    assert "Host" not in headers


def test_post_json_classifies_lost_response_after_body_sent_as_uncertain_retry(
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

    with pytest.raises(RetryableUploadError, match="outcome is uncertain") as exc_info:
        post_json(
            host="127.0.0.1",
            port=48973,
            token="token",
            method="sendMediaGroup",
            payload={"chat_id": "-1001", "media": []},
            file_path=tmp_path / "part.mp4",
        )

    assert exc_info.value.outcome_uncertain is True


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
    assert exc_info.value.outcome_uncertain is False


@pytest.mark.parametrize(
    "body",
    [
        b"\xffSECRET_RESPONSE_TOKEN",
        b"http://localhost/botSECRET_RESPONSE_TOKEN/sendVideo\x1b[31m",
        b"[]",
    ],
    ids=["non-utf8", "invalid-json", "non-object-json"],
)
def test_post_json_marks_malformed_5xx_response_as_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    class BrokenResponse:
        status = 503

        def read(self) -> bytes:
            return body

        def getheader(self, name: str) -> str | None:
            return "6" if name == "Retry-After" else None

    class BrokenConnection:
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

        def getresponse(self) -> BrokenResponse:
            return BrokenResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        BrokenConnection,
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

    assert exc_info.value.retry_after_seconds == 6
    assert exc_info.value.outcome_uncertain is True
    message = str(exc_info.value)
    assert f"response length={len(body)} bytes" in message
    assert "SECRET_RESPONSE_TOKEN" not in message
    assert "\x1b" not in message


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
    headers: list[str] = []
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

        def putheader(self, name: str, value: str) -> None:
            headers.append(name)

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
    assert "Host" not in headers


@pytest.mark.skipif(os.name != "posix", reason="replacing an open file is POSIX-specific")
def test_post_multipart_stats_and_sends_the_same_open_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "video.mp4"
    replacement = tmp_path / "replacement.mp4"
    original_bytes = b"old-data"
    replacement_bytes = b"new-data"
    video.write_bytes(original_bytes)
    replacement.write_bytes(replacement_bytes)
    sent: list[bytes] = []
    headers: dict[str, str] = {}

    class ReplacingConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any) -> None:
            os.replace(replacement, video)

        def putheader(self, name: str, value: str) -> None:
            headers[name] = value

        def endheaders(self) -> None:
            pass

        def send(self, body: bytes) -> None:
            sent.append(body)

        def getresponse(self) -> FakeResponse:
            return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        ReplacingConnection,
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

    request_body = b"".join(sent)
    assert result["ok"] is True
    assert video.read_bytes() == replacement_bytes
    assert original_bytes in request_body
    assert replacement_bytes not in request_body
    assert int(headers["Content-Length"]) == len(request_body)


def test_post_multipart_does_not_echo_malformed_response_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    response_body = b"http://localhost/botSECRET_RESPONSE_TOKEN/sendVideo\x1b[31m\nnot-json"

    class SensitiveResponse:
        status = 503

        def read(self) -> bytes:
            return response_body

        def getheader(self, name: str) -> str | None:
            return None

    class SensitiveConnection:
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

        def getresponse(self) -> SensitiveResponse:
            return SensitiveResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "tg_comment_uploader.cli.http.client.HTTPConnection",
        SensitiveConnection,
    )

    with pytest.raises(RetryableUploadError) as exc_info:
        post_multipart(
            host="127.0.0.1",
            port=48973,
            token="token",
            method="sendVideo",
            fields={"chat_id": "-1001"},
            file_field="video",
            file_path=video,
        )

    message = str(exc_info.value)
    assert exc_info.value.outcome_uncertain is True
    assert f"response length={len(response_body)} bytes" in message
    assert "SECRET_RESPONSE_TOKEN" not in message
    assert "\x1b" not in message


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
