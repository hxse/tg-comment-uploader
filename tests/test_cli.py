import http.client
import json
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.cli import (
    DEFAULT_PROFILE,
    DEFAULT_RETRIES,
    MAX_LOCAL_BOT_API_UPLOAD_BYTES,
    AppConfig,
    AppError,
    BotConfig,
    NonRetryableUploadError,
    ProfileConfig,
    RetryableUploadError,
    ServerConfig,
    build_parser,
    format_bytes,
    format_duration,
    load_config,
    make_bot_api_error,
    make_transport_error,
    print_upload_plan,
    render_caption,
    response_protocol_error,
    run_upload,
    send_video,
    upload_with_retries,
    validate_upload_paths,
)


def write_test_config(
    tmp_path: Path,
    profiles: dict[str, dict[str, Any]],
) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "bot": {
                    "token": "token",
                    "api_id": 1,
                    "api_hash": "hash",
                },
                "server": {
                    "host": "127.0.0.1",
                    "port": 48973,
                    "binary": "telegram-bot-api",
                    "work_dir": ".local/test",
                },
                "profiles": profiles,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_config_supports_reply_and_direct_profiles(tmp_path: Path) -> None:
    config = load_config(
        write_test_config(
            tmp_path,
            {
                "default": {
                    "chat_id": "-1001111111111",
                    "reply_message_id": 12345,
                },
                "direct": {
                    "chat_id": "-1002222222222",
                },
                "direct-null": {
                    "chat_id": "-1003333333333",
                    "reply_message_id": None,
                },
            },
        )
    )

    assert config.profiles["default"].reply_message_id == 12345
    assert config.profiles["direct"].reply_message_id is None
    assert config.profiles["direct-null"].reply_message_id is None


@pytest.mark.parametrize(
    "invalid_value",
    [True, "12345", 1.5, {}],
    ids=["boolean", "string", "float", "object"],
)
def test_load_config_rejects_invalid_optional_reply_message_id(
    tmp_path: Path,
    invalid_value: Any,
) -> None:
    path = write_test_config(
        tmp_path,
        {
            "invalid": {
                "chat_id": "-1001111111111",
                "reply_message_id": invalid_value,
            }
        },
    )

    with pytest.raises(
        AppError,
        match=r"profiles\.invalid\.reply_message_id must be an integer or null",
    ):
        load_config(path)


@pytest.mark.parametrize(
    "reply_message_id",
    [12345, None],
    ids=["reply", "direct"],
)
def test_send_video_includes_reply_parameters_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    reply_message_id: int | None,
) -> None:
    captured_fields: dict[str, str] = {}

    def fake_post_multipart(**kwargs: Any) -> dict[str, Any]:
        fields = kwargs["fields"]
        assert isinstance(fields, dict)
        captured_fields.update(fields)
        return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr("tg_comment_uploader.cli.post_multipart", fake_post_multipart)

    result = send_video(
        fake_config(),
        fake_profile(reply_message_id),
        Path("/videos/a.mp4"),
        "caption",
    )

    assert result == {"message_id": 42}
    assert captured_fields["chat_id"] == "-1001234567890"
    assert captured_fields["supports_streaming"] == "true"
    assert captured_fields["caption"] == "caption"
    if reply_message_id is None:
        assert "reply_parameters" not in captured_fields
    else:
        assert json.loads(captured_fields["reply_parameters"]) == {"message_id": reply_message_id}


def test_print_upload_plan_shows_reply_and_direct_modes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_upload_plan(
        "default",
        fake_profile(12345),
        [Path("/videos/a.mp4")],
    )
    reply_output = capsys.readouterr().out

    print_upload_plan(
        "direct",
        fake_profile(None),
        [Path("/videos/a.mp4")],
    )
    direct_output = capsys.readouterr().out

    assert "delivery: reply" in reply_output
    assert "reply_message_id: 12345" in reply_output
    assert "delivery: direct" in direct_output
    assert "reply_message_id: <none>" in direct_output


def test_render_caption_defaults_to_stem_shape() -> None:
    path = Path("/videos/20260709 sample.mp4")

    assert render_caption("{stem}", path) == "20260709 sample"
    assert render_caption("{name}", path) == "20260709 sample.mp4"


def test_render_caption_rejects_unknown_placeholder() -> None:
    with pytest.raises(AppError, match="unknown caption placeholder"):
        render_caption("{missing}", Path("/videos/a.mp4"))


def test_upload_parser_defaults_to_default_profile_and_five_retries() -> None:
    args = build_parser().parse_args(["upload", "/videos/a.mp4"])

    assert DEFAULT_PROFILE == "default"
    assert DEFAULT_RETRIES == 5
    assert args.profile == "default"
    assert args.retries == 5


def test_just_upload_defaults_to_five_retries() -> None:
    justfile = Path(__file__).parents[1] / "justfile"

    content = justfile.read_text(encoding="utf-8")
    assert 'profile := "default"' in content
    assert 'retries := "5"' in content


def test_validate_upload_paths_requires_absolute_path() -> None:
    with pytest.raises(AppError, match="must be absolute"):
        validate_upload_paths(["relative.mp4"])


def test_validate_upload_paths_preserves_order(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    assert validate_upload_paths([str(first), str(second)]) == [first, second]


def test_validate_upload_paths_enforces_exact_size_limit(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    at_limit = tmp_path / "at-limit.mp4"
    oversized = tmp_path / "oversized.mp4"

    empty.touch()
    with at_limit.open("wb") as file:
        file.truncate(MAX_LOCAL_BOT_API_UPLOAD_BYTES)
    with oversized.open("wb") as file:
        file.truncate(MAX_LOCAL_BOT_API_UPLOAD_BYTES + 1)

    with pytest.raises(NonRetryableUploadError, match="upload file is empty"):
        validate_upload_paths([str(empty)])

    assert validate_upload_paths([str(at_limit)]) == [at_limit]

    with pytest.raises(NonRetryableUploadError) as exc_info:
        validate_upload_paths([str(oversized)])

    message = str(exc_info.value)
    assert str(oversized) in message
    assert f"{MAX_LOCAL_BOT_API_UPLOAD_BYTES + 1:,} bytes" in message
    assert "limit=2000 MiB (2,097,152,000 bytes)" in message
    assert "no upload was attempted" in message


def test_run_upload_preflights_all_sizes_before_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    oversized = tmp_path / "oversized.mp4"
    first.write_bytes(b"video")
    with oversized.open("wb") as file:
        file.truncate(MAX_LOCAL_BOT_API_UPLOAD_BYTES + 1)

    args = build_parser().parse_args(
        [
            "upload",
            "--profile",
            "default",
            str(first),
            str(oversized),
        ]
    )
    calls = 0

    def fake_send_video(
        config: AppConfig,
        profile: ProfileConfig,
        path: Path,
        caption: str,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"message_id": 42}

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)

    with pytest.raises(NonRetryableUploadError, match="no upload was attempted"):
        run_upload(args)

    assert calls == 0


def test_upload_with_retries_stops_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_send_video(
        config: AppConfig,
        profile: ProfileConfig,
        path: Path,
        caption: str,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableUploadError("temporary failure")
        return {"message_id": 42}

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)

    result = upload_with_retries(
        fake_config(),
        fake_profile(),
        Path("/videos/a.mp4"),
        "a",
        retries=5,
    )

    assert result == {"message_id": 42}
    assert calls == 3


def test_upload_with_retries_fails_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_send_video(
        config: AppConfig,
        profile: ProfileConfig,
        path: Path,
        caption: str,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RetryableUploadError("still failing")

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)

    with pytest.raises(AppError, match="failed after 3 attempts"):
        upload_with_retries(
            fake_config(),
            fake_profile(),
            Path("/videos/a.mp4"),
            "a",
            retries=2,
        )

    assert calls == 3


def test_non_retryable_upload_error_stops_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_send_video(
        config: AppConfig,
        profile: ProfileConfig,
        path: Path,
        caption: str,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise NonRetryableUploadError("FILE_PARTS_INVALID; not retrying")

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)

    with pytest.raises(NonRetryableUploadError, match="FILE_PARTS_INVALID"):
        upload_with_retries(
            fake_config(),
            fake_profile(),
            Path("/videos/a.mp4"),
            "a",
            retries=5,
        )

    assert calls == 1


def test_retry_after_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[int] = []

    def fake_send_video(
        config: AppConfig,
        profile: ProfileConfig,
        path: Path,
        caption: str,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableUploadError("rate limited", retry_after_seconds=3)
        return {"message_id": 42}

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    result = upload_with_retries(
        fake_config(),
        fake_profile(),
        Path("/videos/a.mp4"),
        "a",
        retries=1,
    )

    assert result == {"message_id": 42}
    assert sleeps == [3]


def test_bot_api_error_classification(tmp_path: Path) -> None:
    video = tmp_path / "a.mp4"
    video.write_bytes(b"video")

    file_parts_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: FILE_PARTS_INVALID",
        },
        http_status=400,
        file_path=video,
    )
    bad_request_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message to be replied not found",
        },
        http_status=400,
        file_path=video,
    )
    request_timeout_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 408,
            "description": "Request Timeout",
        },
        http_status=408,
        file_path=video,
    )
    server_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 503,
            "description": "Service Unavailable",
        },
        http_status=503,
        file_path=video,
    )
    rate_limit_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 17},
        },
        http_status=429,
        file_path=video,
    )
    header_rate_limit_error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
        },
        http_status=429,
        file_path=video,
        retry_after_header="23",
    )

    assert isinstance(file_parts_error, NonRetryableUploadError)
    assert "file-part count" in str(file_parts_error)
    assert isinstance(bad_request_error, NonRetryableUploadError)
    assert isinstance(request_timeout_error, RetryableUploadError)
    assert isinstance(server_error, RetryableUploadError)
    assert isinstance(rate_limit_error, RetryableUploadError)
    assert rate_limit_error.retry_after_seconds == 17
    assert "retry_after=17s" in str(rate_limit_error)
    assert isinstance(header_rate_limit_error, RetryableUploadError)
    assert header_rate_limit_error.retry_after_seconds == 23


def test_transport_error_classification() -> None:
    before_body_complete = make_transport_error(
        "127.0.0.1",
        48973,
        ConnectionResetError("reset"),
        request_body_sent=False,
        http_status=None,
        retry_after_header=None,
    )
    after_body_complete = make_transport_error(
        "127.0.0.1",
        48973,
        TimeoutError("timed out"),
        request_body_sent=True,
        http_status=None,
        retry_after_header=None,
    )
    known_bad_request = make_transport_error(
        "127.0.0.1",
        48973,
        http.client.IncompleteRead(b""),
        request_body_sent=True,
        http_status=400,
        retry_after_header=None,
    )
    known_rate_limit = make_transport_error(
        "127.0.0.1",
        48973,
        http.client.IncompleteRead(b""),
        request_body_sent=True,
        http_status=429,
        retry_after_header="11",
    )

    assert isinstance(before_body_complete, RetryableUploadError)
    assert isinstance(after_body_complete, NonRetryableUploadError)
    assert "not retrying to avoid a duplicate message" in str(after_body_complete)
    assert isinstance(known_bad_request, NonRetryableUploadError)
    assert isinstance(known_rate_limit, RetryableUploadError)
    assert known_rate_limit.retry_after_seconds == 11


def test_protocol_error_uses_retry_after_header() -> None:
    rate_limit = response_protocol_error(
        429,
        "invalid rate-limit response",
        retry_after_header="13",
    )
    uncertain_success = response_protocol_error(200, "invalid success response")

    assert isinstance(rate_limit, RetryableUploadError)
    assert rate_limit.retry_after_seconds == 13
    assert isinstance(uncertain_success, NonRetryableUploadError)


def fake_config() -> AppConfig:
    return AppConfig(
        bot=BotConfig(token="token", api_id=1, api_hash="hash"),
        server=ServerConfig(
            host="127.0.0.1",
            port=48973,
            binary="telegram-bot-api",
            work_dir=Path("."),
        ),
        profiles={"default": fake_profile()},
    )


def fake_profile(reply_message_id: int | None = 12345) -> ProfileConfig:
    return ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=reply_message_id,
        caption="{stem}",
        supports_streaming=True,
    )


def test_upload_connection_refused_points_to_server_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        bot=BotConfig(token="token", api_id=1, api_hash="hash"),
        server=ServerConfig(
            host="127.0.0.1",
            port=9,
            binary="telegram-bot-api",
            work_dir=Path("."),
        ),
        profiles={"default": fake_profile()},
    )

    video = tmp_path / "a.mp4"
    video.write_bytes(b"video")

    class RefusedConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionRefusedError("refused")

        def close(self) -> None:
            pass

    monkeypatch.setattr("tg_comment_uploader.cli.http.client.HTTPConnection", RefusedConnection)

    with pytest.raises(AppError, match="Start it first in another terminal with: just server"):
        upload_with_retries(
            config,
            fake_profile(),
            video,
            "a",
            retries=5,
        )

    captured = capsys.readouterr()
    assert "attempt 1/6" in captured.out
    assert "attempt 2/6" not in captured.out


def test_progress_format_helpers() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1024 * 1024) == "1.0 MiB"
    assert format_duration(0) == "0:00"
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"
