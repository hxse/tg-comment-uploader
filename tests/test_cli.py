from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tg_comment_uploader.cli import (
    DEFAULT_PROFILE,
    DEFAULT_RETRIES,
    AppConfig,
    AppError,
    BotConfig,
    NonRetryableUploadError,
    ProfileConfig,
    RetryableUploadError,
    build_parser,
    fingerprint_file,
    format_bytes,
    load_config,
    main,
    print_upload_plan,
    render_caption,
    retry_delay_seconds,
    retry_upload,
    run_upload_locked,
    validate_caption_template_shape,
    validate_upload_paths,
)
from tg_comment_uploader.telegram_sender import SAFE_UPLOAD_LIMIT_BYTES, UploadItem
from tg_comment_uploader.media_workflow import PreparedMedia
from tg_comment_uploader.upload_state import (
    MtprotoPaths,
    PendingOwner,
    PendingUploadStore,
)
from tg_comment_uploader.upload_contract import OversizePolicy


def write_config(path: Path) -> None:
    path.write_text(
        """
{
  "bot": {"token": "123456:TEST_TOKEN", "api_id": 123, "api_hash": "hash"},
  "profiles": {
    "default": {
      "chat_id": "-1001234567890",
      "reply_message_id": 42,
      "caption": "{stem}",
      "supports_streaming": false
    },
    "direct": {"chat_id": "@private_test", "caption": null},
    "direct-null": {"chat_id": "-1002", "reply_message_id": null}
  }
"""
        + "\n}\n",
        encoding="utf-8",
    )


def fake_profile(reply_message_id: int | None = 42) -> ProfileConfig:
    return ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=reply_message_id,
        caption="{stem}",
        supports_streaming=False,
    )


def fake_config(profile: ProfileConfig | None = None) -> AppConfig:
    selected = profile or fake_profile()
    return AppConfig(
        bot=BotConfig(token="123456:TEST", api_id=123, api_hash="hash"),
        profiles={"default": selected},
    )


def test_load_config_accepts_only_the_final_schema(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    write_config(config_path)

    config = load_config(config_path)

    assert config.bot == BotConfig(token="123456:TEST_TOKEN", api_id=123, api_hash="hash")
    assert config.profiles["default"].reply_message_id == 42
    assert config.profiles["default"].supports_streaming is False
    assert config.profiles["direct"].caption == ""
    assert config.profiles["direct"].reply_message_id is None
    assert config.profiles["direct-null"].reply_message_id is None
    if os.name == "posix":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_load_config_rejects_unknown_fields_at_every_schema_layer(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    private_value = "PRIVATE_UNKNOWN_VALUE"
    for payload, extra_key in (
        (
            {
                "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
                "profiles": {"default": {"chat_id": "-1001"}},
                "unknown_top_level": private_value,
            },
            "unknown_top_level",
        ),
        (
            {
                "bot": {
                    "token": "1:x",
                    "api_id": 1,
                    "api_hash": "h",
                    "unknown_bot_field": private_value,
                },
                "profiles": {"default": {"chat_id": "-1001"}},
            },
            "unknown_bot_field",
        ),
        (
            {
                "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
                "profiles": {
                    "default": {
                        "chat_id": "-1001",
                        "unknown_profile_field": private_value,
                    }
                },
            },
            "unknown_profile_field",
        ),
    ):
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(
            AppError,
            match=rf"(?s){extra_key}.*Extra inputs are not permitted",
        ) as caught:
            load_config(config_path)
        assert private_value not in str(caught.value)


def test_config_models_are_strict_frozen_and_hide_inputs() -> None:
    for model in (BotConfig, ProfileConfig, AppConfig):
        assert model.model_config["strict"] is True
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
        assert model.model_config["hide_input_in_errors"] is True

    bot = BotConfig(token="123456:TEST", api_id=123, api_hash="hash")
    with pytest.raises(ValidationError, match="frozen"):
        setattr(bot, "api_hash", "changed")


def test_config_rejects_duplicate_json_fields_recursively(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
  "profiles": {
    "default": {
      "chat_id": "-1001",
      "reply_message_id": 42,
      "reply_message_id": null
    }
  }
}
""",
        encoding="utf-8",
    )

    with pytest.raises(AppError, match="duplicate JSON object key 'reply_message_id'"):
        load_config(config_path)


def test_config_enforces_telegram_signed_int32_bounds() -> None:
    maximum = 2_147_483_647

    assert BotConfig(token="1:x", api_id=maximum, api_hash="h").api_id == maximum
    assert ProfileConfig(chat_id="-1001", reply_message_id=maximum).reply_message_id == maximum
    with pytest.raises(ValidationError, match="less than or equal to 2147483647"):
        BotConfig(token="1:x", api_id=maximum + 1, api_hash="h")
    with pytest.raises(ValidationError, match="less than or equal to 2147483647"):
        ProfileConfig(chat_id="-1001", reply_message_id=maximum + 1)


def test_load_config_accepts_string_and_integer_chat_ids(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
  "profiles": {
    "string": {"chat_id": "-1001"},
    "integer": {"chat_id": -1002}
  }
}
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.profiles["string"].chat_id == "-1001"
    assert config.profiles["integer"].chat_id == "-1002"


def test_load_config_validation_does_not_echo_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    token_value = 987_654_321_987
    api_hash_value = "PRIVATE_API_HASH_VALUE"
    config_path.write_text(
        """
{
  "bot": {
    "token": 987654321987,
    "api_id": 1,
    "api_hash": ["PRIVATE_API_HASH_VALUE"]
  },
  "profiles": {"default": {"chat_id": "-1001"}}
}
""",
        encoding="utf-8",
    )

    with pytest.raises(AppError) as caught:
        load_config(config_path)

    message = str(caught.value)
    assert str(token_value) not in message
    assert api_hash_value not in message
    assert "bot.token" in message
    assert "bot.api_hash" in message


def test_load_config_rejects_malformed_token_without_echoing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    malformed_token = "PRIVATE_MALFORMED_TOKEN_VALUE"
    config_path.write_text(
        """
{
  "bot": {
    "token": "PRIVATE_MALFORMED_TOKEN_VALUE",
    "api_id": 1,
    "api_hash": "h"
  },
  "profiles": {"default": {"chat_id": "-1001"}}
}
""",
        encoding="utf-8",
    )

    with pytest.raises(AppError) as caught:
        load_config(config_path)

    message = str(caught.value)
    assert malformed_token not in message
    assert "bot.token" in message
    assert "valid bot ID prefix" in message


@pytest.mark.parametrize("invalid", [True, "42", 1.5, [], 0, -1])
def test_load_config_rejects_non_positive_integer_reply_id(
    tmp_path: Path,
    invalid: object,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
  "profiles": {"invalid": {"chat_id": "-1001", "reply_message_id": %s}}
}
"""
        % json.dumps(invalid),
        encoding="utf-8",
    )

    with pytest.raises(AppError, match=r"reply_message_id"):
        load_config(config_path)


@pytest.mark.parametrize("invalid", [True, "1", 0, -1])
def test_load_config_rejects_non_positive_integer_api_id(
    tmp_path: Path,
    invalid: object,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "bot": {"token": "1:x", "api_id": %s, "api_hash": "h"},
  "profiles": {"default": {"chat_id": "-1001"}}
}
"""
        % __import__("json").dumps(invalid),
        encoding="utf-8",
    )

    with pytest.raises(AppError, match=r"api_id"):
        load_config(config_path)


@pytest.mark.parametrize(
    "invalid",
    [
        " ",
        " -1001",
        "-1001 ",
        "0",
        "-0",
        "Display Name",
        "https://example.com/not-telegram",
        "+8613800000000",
    ],
)
def test_load_config_rejects_locally_invalid_chat_id(
    tmp_path: Path,
    invalid: str,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
{
  "bot": {"token": "1:x", "api_id": 1, "api_hash": "h"},
  "profiles": {"default": {"chat_id": %s}}
}
"""
        % __import__("json").dumps(invalid),
        encoding="utf-8",
    )

    with pytest.raises(AppError, match=r"chat_id is invalid"):
        load_config(config_path)


def test_parser_keeps_current_upload_and_pending_command_contract() -> None:
    parser = build_parser()
    args = parser.parse_args(["upload", "-o", "split", "/videos/a.mp4"])

    assert args.profile == DEFAULT_PROFILE
    assert args.retries == DEFAULT_RETRIES == 5
    assert args.oversize_policy == "split"
    assert args.paths == ["/videos/a.mp4"]
    assert parser.parse_args(["pending-status"]).config == Path("config/config.json")
    discard = parser.parse_args(["pending-discard", "--operation-id", "abc"])
    assert discard.operation_id == "abc"
    assert discard.force_foreign_owner is False
    forced = parser.parse_args(
        ["pending-discard", "--operation-id", "abc", "--force-foreign-owner"]
    )
    assert forced.force_foreign_owner is True


def test_main_has_stable_success_expected_failure_and_interrupt_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaticParser:
        def __init__(self, func: Any) -> None:
            self.func = func

        def parse_args(self, argv: list[str] | None) -> argparse.Namespace:
            return argparse.Namespace(func=self.func)

    monkeypatch.setattr(
        "tg_comment_uploader.cli.build_parser", lambda: StaticParser(lambda args: 0)
    )
    assert main(["anything"]) == 0

    def expected_failure(args: argparse.Namespace) -> int:
        raise AppError("bad")

    monkeypatch.setattr(
        "tg_comment_uploader.cli.build_parser",
        lambda: StaticParser(expected_failure),
    )
    assert main(["anything"]) == 1

    def interrupted(args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "tg_comment_uploader.cli.build_parser",
        lambda: StaticParser(interrupted),
    )
    assert main(["anything"]) == 130


def test_print_upload_plan_distinguishes_reply_and_direct(
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = [Path("/videos/a.mp4")]
    print_upload_plan("default", fake_profile(42), files, oversize_policy="split")
    reply_output = capsys.readouterr().out
    print_upload_plan("direct", fake_profile(None), files)
    direct_output = capsys.readouterr().out

    assert "delivery: reply" in reply_output
    assert "reply_message_id: 42" in reply_output
    assert "oversize_policy: split" in reply_output
    assert "delivery: direct" in direct_output
    assert "reply_message_id: <none>" in direct_output


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{name}", "video.test.mp4"),
        ("{stem}", "video.test"),
        ("{suffix}", ".mp4"),
        ("{parent}", "folder"),
        ("{path}", "/root/folder/video.test.mp4"),
        ("{{{stem}}}", "{video.test}"),
        ("_*[]<>& {stem}", "_*[]<>& video.test"),
    ],
)
def test_caption_rendering_remains_exact_plain_text(template: str, expected: str) -> None:
    assert render_caption(template, Path("/root/folder/video.test.mp4")) == expected


@pytest.mark.parametrize(
    "template",
    ["{name[0]}", "{name.foo}", "{name!r}", "{name:>10}", "{}", "{unknown}", "{"],
)
def test_caption_template_rejects_non_exact_fields(template: str) -> None:
    with pytest.raises(AppError):
        validate_caption_template_shape(template, context="caption")


def test_upload_path_validation_preserves_order_and_exact_size_limit(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    assert validate_upload_paths([str(second), str(first)]) == [second, first]
    with pytest.raises(AppError, match="absolute"):
        validate_upload_paths(["relative.mp4"])

    oversized = tmp_path / "oversized.mp4"
    with oversized.open("wb") as output:
        output.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    with pytest.raises(NonRetryableUploadError, match="safe Telegram limit"):
        validate_upload_paths([str(oversized)])
    assert validate_upload_paths([str(oversized)], allow_oversized=True) == [oversized]


def test_fingerprint_file_hashes_one_stable_fd_and_reports_progress(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    content = b"abcdef" * 100
    path.write_bytes(content)
    reports: list[tuple[int, int]] = []

    size, digest = fingerprint_file(
        path, progress=lambda sent, total: reports.append((sent, total))
    )

    assert size == len(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert reports[0] == (0, len(content))
    assert reports[-1] == (len(content), len(content))


def test_retry_reuses_action_obeys_wait_and_stops_on_nonretryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[int] = []

    def flaky() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableUploadError("wait", retry_after_seconds=3)
        return 42

    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)
    assert retry_upload(flaky, label="video", retries=1) == 42
    assert calls == 2
    assert sleeps == [3]

    def rejected() -> int:
        raise NonRetryableUploadError("rejected")

    with pytest.raises(NonRetryableUploadError):
        retry_upload(rejected, label="video", retries=5)


def test_retry_exhaustion_and_backoff_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", sleeps.append)

    with pytest.raises(AppError, match="failed after 6 attempts"):
        retry_upload(
            lambda: (_ for _ in ()).throw(RetryableUploadError("temporary")),
            label="video",
            retries=5,
        )

    assert sleeps == [1, 2, 4, 8, 16]
    assert retry_delay_seconds(RetryableUploadError("x"), failed_attempt=99) == 30
    assert (
        retry_delay_seconds(
            RetryableUploadError("x", retry_after_seconds=999_999),
            failed_attempt=1,
        )
        == 86_400
    )


def test_uncertain_retry_explains_persisted_id_without_duplicate_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def flaky() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableUploadError("lost", outcome_uncertain=True)
        return 1

    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", lambda seconds: None)
    assert retry_upload(flaky, label="video", retries=1) == 1
    error = capsys.readouterr().err
    assert "same persisted Telegram random ID(s)" in error
    assert "may create duplicate" not in error


def test_uncertain_exhaustion_keeps_recovery_hint_with_zero_retries() -> None:
    with pytest.raises(
        AppError,
        match=r"remains unconfirmed.*same persisted Telegram random ID\(s\)",
    ):
        retry_upload(
            lambda: (_ for _ in ()).throw(
                RetryableUploadError("response lost", outcome_uncertain=True)
            ),
            label="video",
            retries=0,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (1024, "1.0 KiB"), (1024**3, "1.0 GiB")],
)
def test_format_bytes(value: int, expected: str) -> None:
    assert format_bytes(value) == expected


class FakeSender:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls: list[tuple[UploadItem, int, str]] = []
        self.closed = 0

    def send_video(
        self,
        item: UploadItem,
        *,
        random_id: int,
        progress_callback: Any = None,
        before_final_request: Any = None,
    ) -> int:
        self.calls.append((item, random_id, "video"))
        if self.fail_once and len(self.calls) == 1:
            raise RetryableUploadError("temporary")
        if progress_callback is not None:
            progress_callback(item.expected_size or 1, item.expected_size or 1)
        if before_final_request is not None:
            before_final_request()
        return 700 + len(self.calls)

    def send_media_group(self, *args: Any, **kwargs: Any) -> tuple[int, ...]:
        raise AssertionError("media group was not expected")

    def close(self) -> None:
        self.closed += 1


def install_runtime_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: FakeSender,
) -> tuple[PendingUploadStore, list[dict[str, Any]]]:
    owner = PendingOwner(config_fingerprint="c" * 64, bot_id=123456)
    state = PendingUploadStore(tmp_path / "pending.json", owner=owner)
    paths = MtprotoPaths(
        pending_path=state.path,
        session_path=tmp_path / "sessions/test.session",
        owner=owner,
    )
    constructions: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "tg_comment_uploader.cli.create_pending_store",
        lambda config_path, bot: (paths, state),
    )

    def fake_create_sender(*args: Any, **kwargs: Any) -> FakeSender:
        constructions.append(kwargs)
        kwargs["peer_resolved"](-1001234567890)
        return sender

    monkeypatch.setattr("tg_comment_uploader.cli.create_sender", fake_create_sender)
    return state, constructions


def test_full_single_video_orchestration_persists_before_send_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "source video.mp4"
    video.write_bytes(b"video-content")
    sender = FakeSender(fail_once=True)
    state, constructions = install_runtime_fakes(tmp_path, monkeypatch, sender)
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", lambda seconds: None)
    args = build_parser().parse_args(["upload", "--retries", "1", str(video)])

    assert run_upload_locked(args) == 0

    assert len(constructions) == 1
    assert constructions[0]["peer_resolved"]
    assert len(sender.calls) == 2
    assert sender.calls[0][1] == sender.calls[1][1]
    assert sender.calls[0][0].caption == "source video"
    assert sender.calls[0][0].expected_size == len(b"video-content")
    assert sender.calls[0][0].expected_sha256 == hashlib.sha256(b"video-content").hexdigest()
    assert sender.closed == 1
    assert state.inspect() is None


def test_resume_skips_media_preparation_for_a_durably_completed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    owner = PendingOwner(config_fingerprint="c" * 64, bot_id=123456)
    state = PendingUploadStore(tmp_path / "pending.json", owner=owner)
    paths = MtprotoPaths(
        pending_path=state.path,
        session_path=tmp_path / "sessions/test.session",
        owner=owner,
    )
    prepared_paths: list[Path] = []

    class SequenceSender(FakeSender):
        def __init__(self, *, fail_call: int | None = None, base: int = 100) -> None:
            super().__init__()
            self.fail_call = fail_call
            self.base = base

        def send_video(self, item: UploadItem, **kwargs: Any) -> int:
            random_id = kwargs["random_id"]
            self.calls.append((item, random_id, "video"))
            kwargs["before_final_request"]()
            if self.fail_call == len(self.calls):
                raise RetryableUploadError(
                    "response lost",
                    outcome_uncertain=True,
                    final_request_started=True,
                )
            return self.base + len(self.calls)

    first_sender = SequenceSender(fail_call=2)
    second_sender = SequenceSender(base=200)
    senders = [first_sender, second_sender]

    @contextmanager
    def fake_prepare(
        path: Path,
        policy: OversizePolicy,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        prepared_paths.append(path)
        yield PreparedMedia(source=path, paths=(path,), policy=policy)

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr(
        "tg_comment_uploader.cli.create_pending_store",
        lambda config_path, bot: (paths, state),
    )
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare)

    def fake_create_sender(*args: Any, **kwargs: Any) -> SequenceSender:
        kwargs["peer_resolved"](-1001234567890)
        return senders.pop(0)

    monkeypatch.setattr("tg_comment_uploader.cli.create_sender", fake_create_sender)
    args = build_parser().parse_args(["upload", "--retries", "0", str(first), str(second)])

    with pytest.raises(AppError, match="failed after 1 attempts"):
        run_upload_locked(args)

    checkpoint = state.inspect()
    assert checkpoint is not None
    assert checkpoint.completed_sources == (1,)
    assert prepared_paths == [first, second]

    assert run_upload_locked(args) == 0
    assert prepared_paths == [first, second, second]
    assert len(second_sender.calls) == 1
    assert second_sender.calls[0][0].path == second
    assert state.inspect() is None


def test_all_paths_and_captions_are_validated_before_state_or_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    first.write_bytes(b"video")
    state_created = False

    def forbidden_store(*args: Any, **kwargs: Any) -> Any:
        nonlocal state_created
        state_created = True
        raise AssertionError("state must not be created")

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.create_pending_store", forbidden_store)
    args = build_parser().parse_args(["upload", str(first), str(tmp_path / "missing.mp4")])

    with pytest.raises(AppError, match="does not exist"):
        run_upload_locked(args)
    assert state_created is False

    calls = 0

    def fail_second(template: str, path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AppError("caption failed")
        return "ok"

    second = tmp_path / "second.mp4"
    second.write_bytes(b"video")
    monkeypatch.setattr("tg_comment_uploader.cli.render_caption", fail_second)
    args = build_parser().parse_args(["upload", str(first), str(second)])
    with pytest.raises(AppError, match="caption failed"):
        run_upload_locked(args)
    assert state_created is False


def test_pending_status_and_discard_output_are_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sender = FakeSender()
    state, _ = install_runtime_fakes(tmp_path, monkeypatch, sender)
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr(
        "tg_comment_uploader.cli.upload_instance_lock",
        lambda: __import__("contextlib").nullcontext(),
    )
    status_args = build_parser().parse_args(["pending-status"])
    assert status_args.func(status_args) == 0
    assert "pending upload: <none>" in capsys.readouterr().out

    video = tmp_path / "private-name.mp4"
    video.write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    from tg_comment_uploader.upload_state import (
        FileIdentity,
        SourceIntent,
        UploadIntent,
        UploadUnitSpec,
    )

    file_identity = FileIdentity(str(video), 1, digest)
    pending = state.open_or_create(
        UploadIntent(
            profile_name="default",
            chat_id="-1001",
            reply_message_id=None,
            supports_streaming=True,
            oversize_policy="error",
            sources=(SourceIntent(file_identity, "secret caption"),),
        )
    )
    assert status_args.func(status_args) == 0
    status_output = capsys.readouterr().out
    assert pending.operation_id in status_output
    assert "owner: current" in status_output
    assert "stage: planned" in status_output
    assert "private-name" not in status_output
    assert "secret caption" not in status_output

    unit = state.register_unit(
        UploadUnitSpec(
            key="source:1/single",
            source_index=1,
            preparation="original",
            files=(file_identity,),
        )
    )
    state.bind_peer(-1001)
    state.mark_sending(unit.key)
    state.mark_confirmed(unit.key, (101,))
    assert status_args.func(status_args) == 0
    partially_confirmed_output = capsys.readouterr().out
    assert "stage: partially-confirmed" in partially_confirmed_output
    assert "stage: planned" not in partially_confirmed_output

    state.mark_source_completed(1)
    assert status_args.func(status_args) == 0
    assert "stage: confirmed" in capsys.readouterr().out

    discard_args = build_parser().parse_args(
        ["pending-discard", "--operation-id", pending.operation_id]
    )
    assert discard_args.func(discard_args) == 0
    assert state.inspect() is None
    discarded = capsys.readouterr()
    assert "new Telegram random IDs" in discarded.out
    assert "may create a duplicate Telegram message or media group" in discarded.err


def test_pending_maintenance_can_inspect_and_explicitly_discard_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tg_comment_uploader.upload_state import (
        FileIdentity,
        SourceIntent,
        UploadIntent,
        UploadUnitSpec,
    )

    state_path = tmp_path / "pending.json"
    original_owner = PendingOwner(config_fingerprint="a" * 64, bot_id=111)
    original_store = PendingUploadStore(state_path, owner=original_owner)
    video = tmp_path / "private-name.mp4"
    video.write_bytes(b"x")
    file_identity = FileIdentity(str(video), 1, hashlib.sha256(b"x").hexdigest())
    pending = original_store.open_or_create(
        UploadIntent(
            profile_name="default",
            chat_id="-1001",
            reply_message_id=None,
            supports_streaming=True,
            oversize_policy="error",
            sources=(SourceIntent(file_identity, "private caption"),),
        )
    )
    unit = original_store.register_unit(
        UploadUnitSpec(
            key="source:1/single",
            source_index=1,
            preparation="original",
            files=(file_identity,),
        )
    )
    original_store.bind_peer(-1001)
    original_store.mark_sending(unit.key)

    current_owner = PendingOwner(config_fingerprint="b" * 64, bot_id=222)
    maintenance_store = PendingUploadStore(state_path, owner=current_owner)
    paths = MtprotoPaths(
        pending_path=state_path,
        session_path=tmp_path / "sessions/current.session",
        owner=current_owner,
    )
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr(
        "tg_comment_uploader.cli.create_pending_store",
        lambda config_path, bot: (paths, maintenance_store),
    )
    monkeypatch.setattr(
        "tg_comment_uploader.cli.upload_instance_lock",
        lambda: __import__("contextlib").nullcontext(),
    )

    status_args = build_parser().parse_args(["pending-status"])
    assert status_args.func(status_args) == 0
    status_output = capsys.readouterr().out
    assert pending.operation_id in status_output
    assert "owner: foreign" in status_output
    assert "stage: sending" in status_output
    assert "private-name" not in status_output
    assert "private caption" not in status_output

    unforced = build_parser().parse_args(
        ["pending-discard", "--operation-id", pending.operation_id]
    )
    with pytest.raises(AppError, match="--force-foreign-owner"):
        unforced.func(unforced)
    assert state_path.exists()

    wrong_id = build_parser().parse_args(
        [
            "pending-discard",
            "--operation-id",
            "wrong",
            "--force-foreign-owner",
        ]
    )
    with pytest.raises(AppError, match="operation ID"):
        wrong_id.func(wrong_id)
    assert state_path.exists()

    forced = build_parser().parse_args(
        [
            "pending-discard",
            "--operation-id",
            pending.operation_id,
            "--force-foreign-owner",
        ]
    )
    assert forced.func(forced) == 0
    assert not state_path.exists()
    discarded = capsys.readouterr()
    assert "discarded foreign-owner pending upload" in discarded.out
    assert "may create a duplicate Telegram message or media group" in discarded.err
