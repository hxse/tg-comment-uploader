import http.client
import io
import json
import os
import tomllib
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.cli import (
    DEFAULT_PROFILE,
    DEFAULT_RETRIES,
    MAX_UNTRUSTED_ERROR_TEXT_LENGTH,
    SAFE_UPLOAD_LIMIT_BYTES,
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
    print_progress,
    print_upload_plan,
    render_caption,
    response_protocol_error,
    retry_upload,
    run_upload_locked,
    run_server,
    run_upload,
    sanitize_untrusted_error_text,
    send_file_with_progress,
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable")
def test_load_config_secures_config_file_mode(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        {"default": {"chat_id": "-1001111111111"}},
    )
    path.chmod(0o644)

    load_config(path)

    assert path.stat().st_mode & 0o777 == 0o600


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


@pytest.mark.parametrize(
    "invalid_result",
    [
        None,
        {},
        {"message_id": True},
        {"message_id": "42"},
    ],
    ids=["missing-result", "missing-message-id", "boolean-message-id", "string-message-id"],
)
def test_send_video_requires_a_valid_integer_message_id(
    monkeypatch: pytest.MonkeyPatch,
    invalid_result: Any,
) -> None:
    monkeypatch.setattr(
        "tg_comment_uploader.cli.post_multipart",
        lambda **kwargs: {"ok": True, "result": invalid_result},
    )

    with pytest.raises(RetryableUploadError, match="valid message ID") as exc_info:
        send_video(
            fake_config(),
            fake_profile(),
            Path("/videos/a.mp4"),
            "caption",
        )

    assert exc_info.value.outcome_uncertain is True


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


def test_render_caption_allows_exact_fields_and_escaped_braces() -> None:
    path = Path("/videos/20260709 sample.mp4")

    assert render_caption("{stem}", path) == "20260709 sample"
    assert render_caption("{name}", path) == "20260709 sample.mp4"
    assert render_caption("{{video}} {parent}/{name}", path) == (
        "{video} videos/20260709 sample.mp4"
    )


@pytest.mark.parametrize(
    "template",
    [
        "{missing}",
        "{name[99]}",
        "{name:}",
        "{name.foo}",
        "{name:d}",
        "{name!r}",
        "{}",
        "{name",
    ],
    ids=[
        "unknown",
        "index",
        "empty-format-spec",
        "attribute",
        "format-spec",
        "conversion",
        "automatic-field",
        "malformed",
    ],
)
def test_render_caption_rejects_non_exact_or_malformed_templates(template: str) -> None:
    with pytest.raises(AppError, match="caption template"):
        render_caption(template, Path("/videos/a.mp4"))


def test_upload_parser_defaults_to_default_profile_and_five_retries() -> None:
    args = build_parser().parse_args(["upload", "/videos/a.mp4"])

    assert DEFAULT_PROFILE == "default"
    assert DEFAULT_RETRIES == 5
    assert args.profile == "default"
    assert args.retries == 5
    assert args.oversize_policy == "error"


def test_upload_parser_accepts_short_oversize_policy_alias() -> None:
    args = build_parser().parse_args(["upload", "-o", "split", "/videos/a.mp4"])

    assert args.oversize_policy == "split"


def test_just_upload_defaults_to_five_retries() -> None:
    justfile = Path(__file__).parents[1] / "justfile"

    content = justfile.read_text(encoding="utf-8")
    assert 'profile := "default"' in content
    assert 'retries := "5"' in content


def test_check_uses_locked_non_mutating_dev_tools() -> None:
    project_root = Path(__file__).parents[1]
    justfile = (project_root / "justfile").read_text(encoding="utf-8")
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "uv run ruff format --check" in justfile
    assert "uv run ty check" in justfile
    assert "uvx ruff" not in justfile
    assert "uvx ty" not in justfile
    assert "ruff==0.15.21" in pyproject["dependency-groups"]["dev"]
    assert "ty==0.0.58" in pyproject["dependency-groups"]["dev"]


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
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES)
    with oversized.open("wb") as file:
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)

    with pytest.raises(NonRetryableUploadError, match="upload file is empty"):
        validate_upload_paths([str(empty)])

    assert validate_upload_paths([str(at_limit)]) == [at_limit]

    with pytest.raises(NonRetryableUploadError) as exc_info:
        validate_upload_paths([str(oversized)])
    message = str(exc_info.value)
    assert str(oversized) in message
    assert f"{SAFE_UPLOAD_LIMIT_BYTES + 1:,} bytes" in message
    assert "limit=1.9 GiB (2,000,000,000 bytes)" in message
    assert "--oversize-policy split" in message
    assert "--oversize-policy compress" in message
    assert "No upload was attempted" in message


def test_run_upload_locked_rejects_non_local_server_before_file_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        bot=BotConfig(token="token", api_id=1, api_hash="hash"),
        server=ServerConfig(
            host="example.com",
            port=48973,
            binary="telegram-bot-api",
            work_dir=Path("."),
        ),
        profiles={"default": fake_profile()},
    )
    args = build_parser().parse_args(["upload", "/not/inspected.mp4"])

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: config)

    def fail_validation(*args: Any, **kwargs: Any) -> list[Path]:
        raise AssertionError("upload paths must not be inspected for a remote server")

    monkeypatch.setattr("tg_comment_uploader.cli.validate_upload_paths", fail_validation)

    with pytest.raises(AppError, match="non-local Bot API server"):
        run_upload_locked(args)


def test_run_upload_locked_prerenders_all_captions_before_preparing_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    args = build_parser().parse_args(["upload", str(first), str(second)])
    rendered: list[Path] = []
    prepare_calls = 0

    def fake_render(template: str, path: Path) -> str:
        rendered.append(path)
        if path == second:
            raise AppError("invalid caption for second file")
        return path.stem

    def fail_prepare(*args: Any, **kwargs: Any) -> Any:
        nonlocal prepare_calls
        prepare_calls += 1
        raise AssertionError("media preparation must not start before all captions render")

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.render_caption", fake_render)
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fail_prepare)

    with pytest.raises(AppError, match="invalid caption for second file"):
        run_upload_locked(args)

    assert rendered == [first, second]
    assert prepare_calls == 0


def test_run_upload_preflights_all_sizes_before_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    oversized = tmp_path / "oversized.mp4"
    first.write_bytes(b"video")
    with oversized.open("wb") as file:
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)

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
    monkeypatch.setattr("tg_comment_uploader.cli.upload_instance_lock", nullcontext)

    with pytest.raises(NonRetryableUploadError, match="No upload was attempted"):
        run_upload(args)

    assert calls == 0


def test_upload_with_retries_stops_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
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
        if calls < 3:
            raise RetryableUploadError("temporary failure")
        return {"message_id": 42}

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    result = upload_with_retries(
        fake_config(),
        fake_profile(),
        Path("/videos/a.mp4"),
        "a",
        retries=5,
    )

    assert result == {"message_id": 42}
    assert calls == 3
    assert sleeps == [1, 2]


def test_upload_with_retries_fails_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
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
        raise RetryableUploadError("still failing")

    monkeypatch.setattr("tg_comment_uploader.cli.send_video", fake_send_video)
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    with pytest.raises(AppError, match="failed after 3 attempts"):
        upload_with_retries(
            fake_config(),
            fake_profile(),
            Path("/videos/a.mp4"),
            "a",
            retries=2,
        )

    assert calls == 3
    assert sleeps == [1, 2]


def test_exponential_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []

    def always_fail() -> None:
        raise RetryableUploadError("temporary failure")

    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    with pytest.raises(AppError, match="failed after 8 attempts"):
        retry_upload(always_fail, label="video", retries=7)

    assert sleeps == [1, 2, 4, 8, 16, 30, 30]


def test_uncertain_outcome_warns_before_retrying(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[int] = []

    def action() -> dict[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableUploadError(
                "upload outcome is uncertain",
                outcome_uncertain=True,
            )
        return {"message_id": 42}

    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    result = retry_upload(
        action,
        label="video",
        retries=1,
    )

    captured = capsys.readouterr()
    assert result == {"message_id": 42}
    assert sleeps == [1]
    assert "WARNING: the previous upload may already have succeeded" in captured.err
    assert "duplicate Telegram messages or media groups" in captured.err


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
    assert request_timeout_error.outcome_uncertain is False
    assert isinstance(server_error, RetryableUploadError)
    assert server_error.outcome_uncertain is False
    assert isinstance(rate_limit_error, RetryableUploadError)
    assert rate_limit_error.retry_after_seconds == 17
    assert "retry_after=17s" in str(rate_limit_error)
    assert isinstance(header_rate_limit_error, RetryableUploadError)
    assert header_rate_limit_error.retry_after_seconds == 23


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": True},
        {"ok": 0},
        {"description": "Bad Request: FILE_PARTS_INVALID"},
    ],
    ids=["missing-ok", "ok-true", "ok-zero", "file-parts-without-explicit-false"],
)
def test_retryable_bot_api_response_without_explicit_false_is_uncertain(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    video = tmp_path / "a.mp4"
    video.write_bytes(b"video")

    error = make_bot_api_error(
        payload,
        http_status=503,
        file_path=video,
        retry_after_header="7",
    )

    assert isinstance(error, RetryableUploadError)
    assert error.outcome_uncertain is True
    assert error.retry_after_seconds == 7
    assert "request may already have succeeded" in str(error)


@pytest.mark.parametrize("terminator", ["/sendVideo", "?method=sendVideo", "#fragment", " ", ""])
def test_untrusted_error_text_redacts_bot_token_for_common_terminators(
    terminator: str,
) -> None:
    cleaned = sanitize_untrusted_error_text(f"https://localhost/bot123456:SUPER_SECRET{terminator}")

    assert "123456:SUPER_SECRET" not in cleaned
    assert "/bot<redacted>" in cleaned


def test_untrusted_error_text_escapes_controls_and_truncates() -> None:
    cleaned = sanitize_untrusted_error_text("正常文本\x1b[31m\a\r\n\t\u202e" + ("x" * 1_000))

    assert cleaned.startswith("正常文本\\x1b[31m\\x07\\r\\n\\t\\u202e")
    assert "\x1b" not in cleaned
    assert "\a" not in cleaned
    assert "\r" not in cleaned
    assert "\n" not in cleaned
    assert "\t" not in cleaned
    assert "\u202e" not in cleaned
    assert len(cleaned) == MAX_UNTRUSTED_ERROR_TEXT_LENGTH
    assert cleaned.endswith("...<truncated>")


def test_transport_error_sanitizes_untrusted_protocol_exception() -> None:
    protocol_text = "bad response from /bot123456:SUPER_SECRET/sendVideo\x1b[31m\n" + ("x" * 1_000)

    error = make_transport_error(
        "127.0.0.1",
        48973,
        http.client.HTTPException(protocol_text),
        request_body_sent=True,
        http_status=503,
        retry_after_header="9",
    )

    message = str(error)
    assert isinstance(error, RetryableUploadError)
    assert error.outcome_uncertain is True
    assert error.retry_after_seconds == 9
    assert "123456:SUPER_SECRET" not in message
    assert "/bot<redacted>/sendVideo" in message
    assert "\x1b" not in message
    assert "\n" not in message
    assert "\\x1b" in message
    assert "\\n" in message
    assert "...<truncated>" in message


def test_bot_api_description_is_sanitized_without_changing_file_parts_classification(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    description = "/bot123456:SUPER_SECRET/sendVideo\a" + ("x" * 600) + " FILE_PARTS_INVALID"

    error = make_bot_api_error(
        {
            "ok": False,
            "error_code": 400,
            "description": description,
        },
        http_status=400,
        file_path=video,
    )

    message = str(error)
    assert isinstance(error, NonRetryableUploadError)
    assert "file-part count" in message
    assert "123456:SUPER_SECRET" not in message
    assert "/bot<redacted>/sendVideo" in message
    assert "\a" not in message
    assert "\\x07" in message
    assert "...<truncated>" in message


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
    interrupted_server_error = make_transport_error(
        "127.0.0.1",
        48973,
        http.client.IncompleteRead(b""),
        request_body_sent=True,
        http_status=503,
        retry_after_header="7",
    )

    assert isinstance(before_body_complete, RetryableUploadError)
    assert isinstance(known_rate_limit, RetryableUploadError)
    assert isinstance(interrupted_server_error, RetryableUploadError)
    assert before_body_complete.outcome_uncertain is False
    assert known_rate_limit.outcome_uncertain is True
    assert interrupted_server_error.retry_after_seconds == 7
    assert interrupted_server_error.outcome_uncertain is True
    assert isinstance(after_body_complete, RetryableUploadError)
    assert after_body_complete.outcome_uncertain is True
    assert "may already have succeeded" in str(after_body_complete)
    assert isinstance(known_bad_request, NonRetryableUploadError)
    assert known_rate_limit.retry_after_seconds == 11


def test_protocol_error_uses_retry_after_header() -> None:
    rate_limit = response_protocol_error(
        429,
        "invalid rate-limit response",
        retry_after_header="13",
    )
    uncertain_success = response_protocol_error(200, "invalid success response")
    malformed_server_error = response_protocol_error(
        503,
        "invalid server-error response",
        retry_after_header="5",
    )

    assert isinstance(rate_limit, RetryableUploadError)
    assert rate_limit.retry_after_seconds == 13
    assert rate_limit.outcome_uncertain is True
    assert isinstance(uncertain_success, RetryableUploadError)
    assert uncertain_success.outcome_uncertain is True
    assert isinstance(malformed_server_error, RetryableUploadError)
    assert malformed_server_error.retry_after_seconds == 5
    assert malformed_server_error.outcome_uncertain is True


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


def test_upload_connection_refused_retries_and_points_to_server_command(
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
    sleeps: list[int] = []
    video.write_bytes(b"video")

    class RefusedConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def putrequest(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionRefusedError("refused")

        def close(self) -> None:
            pass

    monkeypatch.setattr("tg_comment_uploader.cli.http.client.HTTPConnection", RefusedConnection)
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    with pytest.raises(AppError, match="Start it first in another terminal with: just server"):
        upload_with_retries(
            config,
            fake_profile(),
            video,
            "a",
            retries=1,
        )

    captured = capsys.readouterr()
    assert "attempt 1/2" in captured.out
    assert "attempt 2/2" in captured.out
    assert sleeps == [1]


def test_progress_format_helpers() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1024 * 1024) == "1.0 MiB"
    assert format_duration(0) == "0:00"
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"


class ProgressStream(io.StringIO):
    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_upload_progress_tty_pads_shorter_line_to_clear_residual_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = ProgressStream(is_tty=True)
    monkeypatch.setattr("tg_comment_uploader.cli.sys.stdout", stream)
    monkeypatch.setattr("tg_comment_uploader.cli.time.monotonic", lambda: 10.0)

    width = print_progress(
        100,
        100,
        0.0,
        force=True,
        previous_width=160,
    )

    output = stream.getvalue()
    assert width < 160
    assert output.startswith("\rprogress: 100.00%")
    assert output.endswith(" " * (160 - width))


def test_upload_progress_non_tty_is_a_complete_line_without_carriage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = ProgressStream(is_tty=False)
    monkeypatch.setattr("tg_comment_uploader.cli.sys.stdout", stream)
    monkeypatch.setattr("tg_comment_uploader.cli.time.monotonic", lambda: 10.0)

    width = print_progress(50, 100, 0.0, force=True, previous_width=99)

    assert width == 0
    assert "\r" not in stream.getvalue()
    assert stream.getvalue().startswith("progress:  50.00%")
    assert stream.getvalue().endswith("\n")


def test_upload_progress_tty_finishes_line_when_sending_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = ProgressStream(is_tty=True)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    class BrokenConnection:
        def send(self, chunk: bytes) -> None:
            raise RuntimeError("send failed")

    monkeypatch.setattr("tg_comment_uploader.cli.sys.stdout", stream)
    monkeypatch.setattr("tg_comment_uploader.cli.time.monotonic", lambda: 10.0)

    connection: Any = BrokenConnection()
    with video.open("rb") as upload_file:
        with pytest.raises(RuntimeError, match="send failed"):
            send_file_with_progress(
                connection,
                upload_file,
                video,
                video.stat().st_size,
            )

    assert stream.getvalue().startswith("\rprogress:")
    assert stream.getvalue().endswith("\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable")
def test_run_server_uses_child_only_credentials_and_secures_work_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "telegram-bot-api"
    work_dir.mkdir()
    work_dir.chmod(0o755)
    config = AppConfig(
        bot=BotConfig(token="token", api_id=123456, api_hash="test-api-hash"),
        server=ServerConfig(
            host="127.0.0.1",
            port=48973,
            binary="telegram-bot-api",
            work_dir=work_dir,
        ),
        profiles={},
    )
    captured: dict[str, Any] = {}

    class Result:
        returncode = 0

    def fake_run(command: list[str], **kwargs: Any) -> Result:
        captured["command"] = command
        captured.update(kwargs)
        return Result()

    monkeypatch.setenv("TG_TEST_PARENT", "preserved")
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.shutil.which",
        lambda binary: "/usr/bin/telegram-bot-api",
    )
    monkeypatch.setattr("tg_comment_uploader.cli.subprocess.run", fake_run)

    args = build_parser().parse_args(["server", "--config", str(tmp_path / "config.json")])

    assert run_server(args) == 0
    command = captured["command"]
    assert command == [
        "/usr/bin/telegram-bot-api",
        "--local",
        "--http-ip-address",
        "127.0.0.1",
        "--http-port",
        "48973",
    ]
    assert "--api-id" not in command
    assert "--api-hash" not in command
    assert "test-api-hash" not in command
    assert "123456" not in command
    assert captured["cwd"] == work_dir
    assert captured["check"] is False

    child_env = captured["env"]
    assert child_env["TELEGRAM_API_ID"] == "123456"
    assert child_env["TELEGRAM_API_HASH"] == "test-api-hash"
    assert child_env["TG_TEST_PARENT"] == "preserved"
    assert "TELEGRAM_API_ID" not in os.environ
    assert "TELEGRAM_API_HASH" not in os.environ
    assert work_dir.stat().st_mode & 0o777 == 0o700
