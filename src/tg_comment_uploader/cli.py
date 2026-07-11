from __future__ import annotations

import argparse
import errno
import http.client
import json
import mimetypes
import os
import re
import signal
import shutil
import stat
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, cast

from .locking import UploadLockError, upload_instance_lock
from .media_compress import CompressionProgress
from .media_split import SplitProgress
from .media_workflow import MediaPreparationError, OversizePolicy, prepare_media
from .terminal_progress import ResponseWaitIndicator, TerminalProgress

CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.5
UPLOAD_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_PROFILE = "default"
DEFAULT_RETRIES = 5
SAFE_UPLOAD_LIMIT_BYTES = 2_000_000_000
SPLIT_MEDIA_TARGET_BYTES = SAFE_UPLOAD_LIMIT_BYTES * 98 // 100
COMPRESS_MEDIA_TARGET_BYTES = SAFE_UPLOAD_LIMIT_BYTES * 95 // 100
OVERSIZE_POLICIES = ("error", "split", "compress")
OVERSIZE_POLICY_HINT = (
    "Use --oversize-policy split for lossless splitting, or "
    "--oversize-policy compress for lossy compression."
)
MEDIA_GROUP_MAX_ITEMS = 10
RETRY_BACKOFF_INITIAL_SECONDS = 1
RETRY_BACKOFF_MAX_SECONDS = 30
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60
MAX_UNTRUSTED_ERROR_TEXT_LENGTH = 500
BOT_TOKEN_PATH_PATTERN = re.compile(
    r"/bot[^/?#\s]+(?=/|[?#\s]|$)",
    re.IGNORECASE,
)

UploadResultT = TypeVar("UploadResultT")
TRANSIENT_NETWORK_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETRESET,
    errno.ENETUNREACH,
    errno.EPIPE,
    errno.ETIMEDOUT,
}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_CONFIG = Path("config/config.json")


class AppError(Exception):
    """Expected CLI failure with a user-facing message."""


class RetryableUploadError(AppError):
    """Upload failure retried under the CLI's at-least-once delivery policy."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        outcome_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.outcome_uncertain = outcome_uncertain


class NonRetryableUploadError(AppError):
    """Upload failure that can't be fixed by repeating the same request."""


@dataclass(frozen=True)
class BotConfig:
    token: str
    api_id: int
    api_hash: str


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    binary: str
    work_dir: Path


@dataclass(frozen=True)
class ProfileConfig:
    chat_id: str
    reply_message_id: int | None
    caption: str
    supports_streaming: bool


@dataclass(frozen=True)
class AppConfig:
    bot: BotConfig
    server: ServerConfig
    profiles: dict[str, ProfileConfig]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


@contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """Translate SIGTERM into normal Python unwinding during an upload."""

    previous_handler = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handle_sigterm)
    except ValueError:
        # Signal handlers can only be installed by the main thread.
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-comment-uploader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="start local Telegram Bot API server")
    server.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    server.set_defaults(func=run_server)

    upload = subparsers.add_parser("upload", help="upload videos sequentially")
    upload.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    upload.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"profile name (default: {DEFAULT_PROFILE})",
    )
    upload.add_argument(
        "--retries",
        type=non_negative_int,
        default=DEFAULT_RETRIES,
        help="retry count after the first failed attempt for each file",
    )
    upload.add_argument(
        "-o",
        "--oversize-policy",
        choices=OVERSIZE_POLICIES,
        default="error",
        help="how to handle files above the safe upload limit (default: error)",
    )
    upload.add_argument("paths", nargs="+")
    upload.set_defaults(func=run_upload)

    return parser


def run_server(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    server = config.server

    if server.host not in LOCAL_HOSTS:
        raise AppError(f"refuse to bind local Bot API server to non-local address: {server.host!r}")

    binary = shutil.which(server.binary)
    if binary is None:
        raise AppError(f"telegram Bot API server binary not found: {server.binary}")

    try:
        server.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            server.work_dir.chmod(0o700)
    except OSError as exc:
        raise AppError(
            f"failed to prepare private Bot API working directory {server.work_dir}: {exc}"
        ) from exc

    command = [
        binary,
        "--local",
        "--http-ip-address",
        server.host,
        "--http-port",
        str(server.port),
    ]

    child_env = os.environ.copy()
    child_env["TELEGRAM_API_ID"] = str(config.bot.api_id)
    child_env["TELEGRAM_API_HASH"] = config.bot.api_hash
    print(f"starting {server.binary} on http://{server.host}:{server.port}")
    print(f"working directory: {server.work_dir}")
    return subprocess.run(
        command,
        cwd=server.work_dir,
        env=child_env,
        check=False,
    ).returncode


def run_upload(args: argparse.Namespace) -> int:
    try:
        with termination_as_interrupt(), upload_instance_lock():
            return run_upload_locked(args)
    except UploadLockError as exc:
        raise AppError(str(exc)) from exc


def prepared_media_target_bytes(policy: OversizePolicy) -> int:
    if policy == "compress":
        return COMPRESS_MEDIA_TARGET_BYTES
    if policy in {"error", "split"}:
        return SPLIT_MEDIA_TARGET_BYTES
    raise ValueError(f"unsupported oversize policy: {policy}")


def run_upload_locked(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.server.host not in LOCAL_HOSTS:
        raise AppError(
            "refuse to send Bot token or upload files to non-local Bot API server: "
            f"{config.server.host!r}"
        )
    profile = get_profile(config, args.profile)
    oversize_policy = cast(OversizePolicy, args.oversize_policy)
    files = validate_upload_paths(
        args.paths,
        allow_oversized=oversize_policy != "error",
    )
    captions = [render_caption(profile.caption, path) for path in files]

    print_upload_plan(
        args.profile,
        profile,
        files,
        oversize_policy=oversize_policy,
    )

    for index, (path, caption) in enumerate(zip(files, captions, strict=True), start=1):
        prefix = f"[{index}/{len(files)}]"
        renderer = TerminalProgress()
        try:
            renderer.log(f"{prefix} uploading: {path}")
            try:
                with prepare_media(
                    path,
                    oversize_policy,
                    hard_limit_bytes=SAFE_UPLOAD_LIMIT_BYTES,
                    target_bytes=prepared_media_target_bytes(oversize_policy),
                    progress=lambda message: renderer.log(f"{prefix} {message}"),
                    split_progress=lambda event: route_split_progress(renderer, prefix, event),
                    compression_progress=lambda event: route_compression_progress(
                        renderer, prefix, event
                    ),
                    warning=lambda message: print_preparation_warning(renderer, message),
                ) as prepared:
                    if prepared.is_media_group:
                        results = upload_media_groups_with_retries(
                            config,
                            profile,
                            prepared.paths,
                            caption,
                            source=path,
                            retries=args.retries,
                        )
                        message_ids = [
                            str(result.get("message_id", "unknown")) for result in results
                        ]
                        renderer.log(f"{prefix} uploaded message_ids={','.join(message_ids)}")
                    else:
                        result = upload_with_retries(
                            config,
                            profile,
                            prepared.paths[0],
                            caption,
                            retries=args.retries,
                        )
                        message_id = result.get("message_id", "unknown")
                        renderer.log(f"{prefix} uploaded message_id={message_id}")
            except MediaPreparationError as exc:
                raise NonRetryableUploadError(str(exc)) from exc
        finally:
            renderer.finish()

    return 0


def route_split_progress(
    renderer: TerminalProgress,
    prefix: str,
    event: SplitProgress,
) -> None:
    """Route one structured lossless-split event to terminal output."""

    attempt = _progress_count(event.attempt)
    max_attempts = _progress_count(event.max_attempts)
    part_count = _progress_count(event.part_count)
    if event.stage == "probing":
        renderer.log(f"{prefix} probing media for lossless split")
    elif event.stage == "planning":
        renderer.log(
            f"{prefix} planning lossless split attempt {attempt}/{max_attempts} "
            f"({part_count} parts)"
        )
    elif event.stage == "splitting":
        renderer.update(
            f"{prefix} split attempt {attempt}/{max_attempts} ({part_count} parts)",
            event.fraction if event.fraction is not None else 0.0,
        )
    elif event.stage == "validating":
        renderer.log(
            f"{prefix} validating {part_count} split parts from attempt {attempt}/{max_attempts}"
        )
    else:
        raise ValueError(f"unsupported split progress stage: {event.stage}")


def route_compression_progress(
    renderer: TerminalProgress,
    prefix: str,
    event: CompressionProgress,
) -> None:
    """Route one structured two-pass compression event to terminal output."""

    attempt = _progress_count(event.attempt)
    max_attempts = _progress_count(event.max_attempts)
    if event.stage == "probing":
        renderer.log(f"{prefix} probing media for compression")
    elif event.stage == "planning":
        renderer.log(f"{prefix} planning two-pass compression")
    elif event.stage == "compressing":
        renderer.update(
            f"{prefix} compress attempt {attempt}/{max_attempts} "
            f"pass {_progress_count(event.pass_number)}/2",
            event.fraction if event.fraction is not None else 0.0,
        )
    elif event.stage == "validating":
        renderer.log(f"{prefix} validating compressed output from attempt {attempt}/{max_attempts}")
    else:
        raise ValueError(f"unsupported compression progress stage: {event.stage}")


def print_preparation_warning(renderer: TerminalProgress, message: str) -> None:
    """Keep a preparation warning separate from an active progress line."""

    renderer.finish()
    print(f"warning: {message}", file=sys.stderr)


def _progress_count(value: int | None) -> str:
    return str(value) if value is not None else "?"


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise AppError(f"config file does not exist: {path}")
    if not path.is_file():
        raise AppError(f"config path is not a file: {path}")

    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise AppError(f"failed to secure config file {path} with mode 0600: {exc}") from exc

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AppError("config root must be an object")

    bot_raw = require_object(raw, "bot")
    server_raw = require_object(raw, "server")
    profiles_raw = require_object(raw, "profiles")

    bot = BotConfig(
        token=require_non_empty_string(bot_raw, "token"),
        api_id=require_int(bot_raw, "api_id"),
        api_hash=require_non_empty_string(bot_raw, "api_hash"),
    )

    port = require_int(server_raw, "port")
    if port < 1 or port > 65535:
        raise AppError("server.port must be between 1 and 65535")

    work_dir = Path(require_non_empty_string(server_raw, "work_dir"))
    if not work_dir.is_absolute():
        work_dir = Path.cwd() / work_dir

    server = ServerConfig(
        host=require_non_empty_string(server_raw, "host"),
        port=port,
        binary=str(server_raw.get("binary", "telegram-bot-api")),
        work_dir=work_dir,
    )

    profiles: dict[str, ProfileConfig] = {}
    for name, profile_raw in profiles_raw.items():
        if not isinstance(name, str) or not name:
            raise AppError("profile names must be non-empty strings")
        if not isinstance(profile_raw, dict):
            raise AppError(f"profiles.{name} must be an object")

        caption = profile_raw.get("caption", "{stem}")
        if caption is None:
            caption = ""
        if not isinstance(caption, str):
            raise AppError(f"profiles.{name}.caption must be a string or null")
        validate_caption_template(caption, name)

        supports_streaming = profile_raw.get("supports_streaming", True)
        if not isinstance(supports_streaming, bool):
            raise AppError(f"profiles.{name}.supports_streaming must be a boolean")

        profiles[name] = ProfileConfig(
            chat_id=require_non_empty_string(profile_raw, "chat_id", prefix=f"profiles.{name}"),
            reply_message_id=optional_int(
                profile_raw, "reply_message_id", prefix=f"profiles.{name}"
            ),
            caption=caption,
            supports_streaming=supports_streaming,
        )

    return AppConfig(bot=bot, server=server, profiles=profiles)


def get_profile(config: AppConfig, profile_name: str) -> ProfileConfig:
    try:
        return config.profiles[profile_name]
    except KeyError as exc:
        available = ", ".join(sorted(config.profiles)) or "<none>"
        raise AppError(
            f"unknown profile {profile_name!r}; available profiles: {available}"
        ) from exc


def validate_upload_paths(
    paths: list[str],
    *,
    allow_oversized: bool = False,
) -> list[Path]:
    validated: list[Path] = []

    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            raise AppError(f"upload path must be absolute: {raw_path}")
        if not path.exists():
            raise AppError(f"upload file does not exist: {path}")
        if not path.is_file():
            raise AppError(f"upload path is not a regular file: {path}")
        file_size = get_upload_file_size(path)
        if file_size == 0:
            raise NonRetryableUploadError(f"upload file is empty: {path}; no upload was attempted")
        if not allow_oversized:
            validate_upload_file_size(path, file_size)
        validated.append(path)

    return validated


def get_upload_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to read upload file metadata for {path}: {exc}; not retrying"
        ) from exc


def open_upload_file(path: Path) -> tuple[BinaryIO, int]:
    try:
        upload_file = path.open("rb")
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to open upload file {path}: {exc}; not retrying"
        ) from exc

    try:
        try:
            metadata = os.fstat(upload_file.fileno())
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to read upload file metadata for {path}: {exc}; not retrying"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise NonRetryableUploadError(
                f"upload path is not a regular file: {path}; not retrying"
            )
        file_size = metadata.st_size
        validate_upload_file_size(path, file_size)
    except BaseException:
        try:
            upload_file.close()
        except OSError:
            pass
        raise

    return upload_file, file_size


def validate_upload_file_size(path: Path, file_size: int) -> None:
    if file_size == 0:
        raise NonRetryableUploadError(f"upload file is empty: {path}; no upload was attempted")
    if file_size <= SAFE_UPLOAD_LIMIT_BYTES:
        return

    raise NonRetryableUploadError(
        f"upload file is too large for Telegram local Bot API: {path}; "
        f"size={format_bytes(file_size)} ({file_size:,} bytes), "
        f"limit={format_bytes(SAFE_UPLOAD_LIMIT_BYTES)} "
        f"({SAFE_UPLOAD_LIMIT_BYTES:,} bytes). "
        f"{OVERSIZE_POLICY_HINT} No upload was attempted."
    )


def print_upload_plan(
    profile_name: str,
    profile: ProfileConfig,
    files: list[Path],
    *,
    oversize_policy: str = "error",
) -> None:
    print(f"profile: {profile_name}")
    print(f"chat_id: {profile.chat_id}")
    print(f"oversize_policy: {oversize_policy}")
    if profile.reply_message_id is None:
        print("delivery: direct")
        print("reply_message_id: <none>")
    else:
        print("delivery: reply")
        print(f"reply_message_id: {profile.reply_message_id}")
    print("files:")
    for index, path in enumerate(files, start=1):
        print(f"  {index}. {path}")


def render_caption(template: str, path: Path) -> str:
    validate_caption_template_shape(template, context="caption template")
    values = {
        "name": path.name,
        "stem": path.stem,
        "suffix": path.suffix,
        "parent": path.parent.name,
        "path": str(path),
    }
    try:
        return template.format(**values)
    except (KeyError, IndexError, AttributeError, ValueError) as exc:
        raise AppError(f"failed to render caption template {template!r}: {exc}") from exc


def validate_caption_template(template: str, profile_name: str) -> None:
    validate_caption_template_shape(
        template,
        context=f"profiles.{profile_name}.caption",
    )


def validate_caption_template_shape(template: str, *, context: str) -> None:
    allowed = {"name", "stem", "suffix", "parent", "path"}
    index = 0
    while index < len(template):
        character = template[index]
        if character == "{":
            if index + 1 < len(template) and template[index + 1] == "{":
                index += 2
                continue
            closing = template.find("}", index + 1)
            if closing < 0:
                raise AppError(f"{context} is invalid: unmatched '{{' at position {index}")
            field_name = template[index + 1 : closing]
            if field_name not in allowed:
                raise AppError(
                    f"{context} contains unsupported placeholder {{{field_name}}}; "
                    f"allowed exact fields: {', '.join(sorted(allowed))}"
                )
            index = closing + 1
            continue
        if character == "}":
            if index + 1 < len(template) and template[index + 1] == "}":
                index += 2
                continue
            raise AppError(f"{context} is invalid: unmatched '}}' at position {index}")
        index += 1


def send_video(
    config: AppConfig,
    profile: ProfileConfig,
    path: Path,
    caption: str,
) -> dict[str, Any]:
    fields = {
        "chat_id": profile.chat_id,
        "supports_streaming": "true" if profile.supports_streaming else "false",
    }
    if profile.reply_message_id is not None:
        fields["reply_parameters"] = json.dumps(
            {"message_id": profile.reply_message_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if caption:
        fields["caption"] = caption

    response = post_multipart(
        host=config.server.host,
        port=config.server.port,
        token=config.bot.token,
        method="sendVideo",
        fields=fields,
        file_field="video",
        file_path=path,
    )

    if response.get("ok") is not True:
        raise make_bot_api_error(response, http_status=None, file_path=path)

    result = response.get("result")
    if not isinstance(result, dict) or not is_message_id(result.get("message_id")):
        raise uncertain_upload_error(
            f"upload outcome is uncertain for {path}: "
            "Bot API reported success without a valid message ID"
        )
    return result


def build_media_group_payload(
    profile: ProfileConfig,
    paths: Sequence[Path],
    caption: str,
) -> dict[str, Any]:
    if len(paths) < 2 or len(paths) > MEDIA_GROUP_MAX_ITEMS:
        raise ValueError(f"a Telegram media group must contain 2-{MEDIA_GROUP_MAX_ITEMS} videos")

    media: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        file_size = get_upload_file_size(path)
        validate_upload_file_size(path, file_size)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to resolve media-group file {path}: {exc}; not retrying"
            ) from exc
        if not resolved.is_file():
            raise NonRetryableUploadError(
                f"media-group path is not a regular file: {resolved}; not retrying"
            )

        item: dict[str, Any] = {
            "type": "video",
            "media": resolved.as_uri(),
            "supports_streaming": profile.supports_streaming,
        }
        if index == 0 and caption:
            item["caption"] = caption
        media.append(item)

    payload: dict[str, Any] = {
        "chat_id": profile.chat_id,
        "media": media,
    }
    if profile.reply_message_id is not None:
        payload["reply_parameters"] = {"message_id": profile.reply_message_id}
    return payload


def send_media_group(
    config: AppConfig,
    profile: ProfileConfig,
    paths: Sequence[Path],
    caption: str,
) -> list[dict[str, Any]]:
    payload = build_media_group_payload(profile, paths, caption)
    response = post_json(
        host=config.server.host,
        port=config.server.port,
        token=config.bot.token,
        method="sendMediaGroup",
        payload=payload,
        file_path=paths[0],
    )

    if response.get("ok") is not True:
        raise make_bot_api_error(response, http_status=None, file_path=paths[0])

    result = response.get("result")
    if (
        not isinstance(result, list)
        or len(result) != len(paths)
        or not all(isinstance(item, dict) for item in result)
        or not all(is_message_id(item.get("message_id")) for item in result)
    ):
        raise uncertain_upload_error(
            f"upload outcome is uncertain for media group starting with {paths[0]}: "
            "Bot API reported success without the expected valid message IDs"
        )
    return cast(list[dict[str, Any]], result)


def is_message_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def partition_media_groups(paths: Sequence[Path]) -> tuple[tuple[Path, ...], ...]:
    if len(paths) < 2:
        raise ValueError("at least two paths are required for media-group partitioning")

    group_count = (len(paths) + MEDIA_GROUP_MAX_ITEMS - 1) // MEDIA_GROUP_MAX_ITEMS
    base_size, larger_group_count = divmod(len(paths), group_count)
    if base_size < 2 or base_size + (1 if larger_group_count else 0) > MEDIA_GROUP_MAX_ITEMS:
        raise ValueError("cannot partition paths into valid Telegram media groups")

    groups: list[tuple[Path, ...]] = []
    offset = 0
    for group_index in range(group_count):
        group_size = base_size + (1 if group_index < larger_group_count else 0)
        groups.append(tuple(paths[offset : offset + group_size]))
        offset += group_size
    return tuple(groups)


def upload_media_groups_with_retries(
    config: AppConfig,
    profile: ProfileConfig,
    paths: Sequence[Path],
    caption: str,
    *,
    source: Path,
    retries: int,
) -> list[dict[str, Any]]:
    groups = partition_media_groups(paths)
    results: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups, start=1):
        label = f"{source} media group {group_index}/{len(groups)}"
        print(f"uploading media group {group_index}/{len(groups)} ({len(group)} videos): {source}")
        try:
            group_result = retry_upload(
                lambda group=group: send_media_group(config, profile, group, caption),
                label=label,
                retries=retries,
            )
        except AppError as exc:
            if group_index > 1:
                uploaded_count = len(results)
                raise AppError(
                    f"upload partially succeeded for {source}: {uploaded_count} split "
                    f"video message(s) in {group_index - 1} media group(s) were already "
                    f"sent before media group {group_index}/{len(groups)} failed; "
                    f"rerunning the whole command may duplicate them; current error: {exc}"
                ) from exc
            raise
        results.extend(group_result)
    return results


def upload_with_retries(
    config: AppConfig,
    profile: ProfileConfig,
    path: Path,
    caption: str,
    *,
    retries: int,
) -> dict[str, Any]:
    return retry_upload(
        lambda: send_video(config, profile, path, caption),
        label=str(path),
        retries=retries,
    )


def retry_upload(
    action: Callable[[], UploadResultT],
    *,
    label: str,
    retries: int,
) -> UploadResultT:
    if retries < 0:
        raise ValueError("retries must be non-negative")

    attempts = retries + 1
    last_error: RetryableUploadError | None = None

    for attempt in range(1, attempts + 1):
        try:
            if attempts > 1:
                print(f"attempt {attempt}/{attempts}: {label}")
            return action()
        except RetryableUploadError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(f"attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
            if exc.outcome_uncertain:
                print(
                    "WARNING: the previous upload may already have succeeded; retrying may "
                    "create duplicate Telegram messages or media groups",
                    file=sys.stderr,
                )
            delay = retry_delay_seconds(exc, failed_attempt=attempt)
            delay_source = (
                "Bot API retry_after"
                if exc.retry_after_seconds is not None
                else "exponential backoff"
            )
            print(f"waiting {delay}s before retrying ({delay_source})", file=sys.stderr)
            time.sleep(delay)

    assert last_error is not None
    raise AppError(
        f"upload failed after {attempts} attempts for {label}: {last_error}"
    ) from last_error


def retry_delay_seconds(error: RetryableUploadError, *, failed_attempt: int) -> int:
    if error.retry_after_seconds is not None:
        return error.retry_after_seconds

    exponent = min(max(failed_attempt - 1, 0), 30)
    delay = RETRY_BACKOFF_INITIAL_SECONDS * (1 << exponent)
    return min(delay, RETRY_BACKOFF_MAX_SECONDS)


def post_multipart(
    *,
    host: str,
    port: int,
    token: str,
    method: str,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> dict[str, Any]:
    boundary = f"tg-comment-uploader-{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    field_parts = [multipart_field(boundary, key, value) for key, value in fields.items()]
    file_header = multipart_file_header(boundary, file_field, file_path.name, content_type)
    closing = f"\r\n--{boundary}--\r\n".encode()

    connection = http.client.HTTPConnection(host, port, timeout=UPLOAD_TIMEOUT_SECONDS)
    upload_file: BinaryIO | None = None
    request_body_sent = False
    response: http.client.HTTPResponse | None = None
    body: bytes | None = None
    try:
        upload_file, file_size = open_upload_file(file_path)
        content_length = (
            sum(len(part) for part in field_parts) + len(file_header) + file_size + len(closing)
        )

        connection.putrequest("POST", f"/bot{token}/{method}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()

        for part in field_parts:
            connection.send(part)
        connection.send(file_header)
        send_file_with_progress(connection, upload_file, file_path, file_size)
        connection.send(closing)
        request_body_sent = True

        with ResponseWaitIndicator():
            response = connection.getresponse()
            body = response.read()
    except (OSError, http.client.HTTPException) as exc:
        http_status = response.status if response is not None else None
        retry_after_header = response.getheader("Retry-After") if response is not None else None
        raise make_transport_error(
            host,
            port,
            exc,
            request_body_sent=request_body_sent,
            http_status=http_status,
            retry_after_header=retry_after_header,
        ) from exc
    finally:
        if upload_file is not None:
            try:
                upload_file.close()
            except OSError:
                pass
        try:
            connection.close()
        except OSError:
            pass

    assert response is not None
    assert body is not None

    try:
        payload = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-UTF-8 response with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc
    except json.JSONDecodeError as exc:
        raise response_protocol_error(
            response.status,
            f"Bot API returned invalid JSON with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc

    if not isinstance(payload, dict):
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-object JSON response with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        )

    if response.status < 200 or response.status >= 300:
        raise make_bot_api_error(
            payload,
            http_status=response.status,
            file_path=file_path,
            retry_after_header=response.getheader("Retry-After"),
        )

    return payload


def post_json(
    *,
    host: str,
    port: int,
    token: str,
    method: str,
    payload: dict[str, Any],
    file_path: Path,
) -> dict[str, Any]:
    body_to_send = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    connection = http.client.HTTPConnection(host, port, timeout=UPLOAD_TIMEOUT_SECONDS)
    request_body_sent = False
    response: http.client.HTTPResponse | None = None
    body: bytes | None = None
    try:
        connection.putrequest("POST", f"/bot{token}/{method}")
        connection.putheader("Content-Type", "application/json; charset=utf-8")
        connection.putheader("Content-Length", str(len(body_to_send)))
        connection.endheaders()
        connection.send(body_to_send)
        request_body_sent = True

        with ResponseWaitIndicator():
            response = connection.getresponse()
            body = response.read()
    except (OSError, http.client.HTTPException) as exc:
        http_status = response.status if response is not None else None
        retry_after_header = response.getheader("Retry-After") if response is not None else None
        raise make_transport_error(
            host,
            port,
            exc,
            request_body_sent=request_body_sent,
            http_status=http_status,
            retry_after_header=retry_after_header,
        ) from exc
    finally:
        connection.close()

    assert response is not None
    assert body is not None

    try:
        decoded = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-UTF-8 response with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc
    except json.JSONDecodeError as exc:
        raise response_protocol_error(
            response.status,
            f"Bot API returned invalid JSON with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc

    if not isinstance(decoded, dict):
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-object JSON response with HTTP {response.status}; "
            f"response length={len(body)} bytes",
            retry_after_header=response.getheader("Retry-After"),
        )

    if response.status < 200 or response.status >= 300:
        raise make_bot_api_error(
            decoded,
            http_status=response.status,
            file_path=file_path,
            retry_after_header=response.getheader("Retry-After"),
        )

    return decoded


def sanitize_untrusted_error_text(text: str) -> str:
    redacted = BOT_TOKEN_PATH_PATTERN.sub("/bot<redacted>", text)
    escaped = "".join(
        character if character.isprintable() else ascii(character)[1:-1] for character in redacted
    )
    if len(escaped) <= MAX_UNTRUSTED_ERROR_TEXT_LENGTH:
        return escaped

    suffix = "...<truncated>"
    return escaped[: MAX_UNTRUSTED_ERROR_TEXT_LENGTH - len(suffix)] + suffix


def response_protocol_error(
    http_status: int,
    message: str,
    *,
    retry_after_header: str | None = None,
) -> AppError:
    message = sanitize_untrusted_error_text(message)
    if is_retryable_status(http_status):
        return uncertain_upload_error(
            message,
            retry_after_seconds=parse_retry_after(retry_after_header),
        )
    if 200 <= http_status < 300:
        return uncertain_upload_error(message)
    return NonRetryableUploadError(f"{message}; not retrying")


def uncertain_upload_error(
    message: str,
    *,
    retry_after_seconds: int | None = None,
) -> RetryableUploadError:
    return RetryableUploadError(
        f"{message}; the request may already have succeeded",
        retry_after_seconds=retry_after_seconds,
        outcome_uncertain=True,
    )


def make_transport_error(
    host: str,
    port: int,
    error: OSError | http.client.HTTPException,
    *,
    request_body_sent: bool,
    http_status: int | None,
    retry_after_header: str | None,
) -> AppError:
    error_text = sanitize_untrusted_error_text(str(error))
    if http_status is not None:
        message = f"Bot API response failed with HTTP {http_status}: {error_text}"
        if is_retryable_status(http_status):
            return uncertain_upload_error(
                message,
                retry_after_seconds=parse_retry_after(retry_after_header),
            )
        if 200 <= http_status < 300:
            return uncertain_upload_error(f"{message}; upload outcome is uncertain")
        return NonRetryableUploadError(f"{message}; request rejected; not retrying")

    if request_body_sent:
        return uncertain_upload_error(
            f"upload request body was fully sent to {host}:{port}, but no response was received: "
            f"{error_text}; upload outcome is uncertain"
        )

    if isinstance(error, ConnectionRefusedError):
        return RetryableUploadError(
            f"local Bot API server is not running at {host}:{port}. "
            "Start it first in another terminal with: just server"
        )

    if isinstance(error, socket.gaierror):
        return NonRetryableUploadError(
            f"failed to resolve local Bot API server host {host!r}: {error_text}; not retrying"
        )

    if is_transient_transport_error(error):
        return RetryableUploadError(
            f"transient error while sending to local Bot API server at {host}:{port}: {error_text}"
        )

    return NonRetryableUploadError(
        f"non-transient error while calling local Bot API server at {host}:{port}: "
        f"{error_text}; not retrying"
    )


def is_transient_transport_error(error: OSError | http.client.HTTPException) -> bool:
    if isinstance(
        error,
        (TimeoutError, ConnectionAbortedError, ConnectionResetError, BrokenPipeError),
    ):
        return True
    return isinstance(error, OSError) and error.errno in TRANSIENT_NETWORK_ERRNOS


def parse_retry_after(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        try:
            seconds = int(value)
        except ValueError:
            return None
    else:
        return None

    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def make_bot_api_error(
    payload: dict[str, Any],
    *,
    http_status: int | None,
    file_path: Path,
    retry_after_header: str | None = None,
) -> AppError:
    description_raw = payload.get("description", "unknown Bot API error")
    description_text = (
        description_raw if isinstance(description_raw, str) else repr(description_raw)
    )
    description = sanitize_untrusted_error_text(description_text)
    error_code_raw = payload.get("error_code")
    error_code = (
        error_code_raw
        if isinstance(error_code_raw, int) and not isinstance(error_code_raw, bool)
        else None
    )

    context: list[str] = []
    if http_status is not None:
        context.append(f"HTTP {http_status}")
    if error_code is not None:
        context.append(f"error_code={error_code}")
    label = f"Bot API ({', '.join(context)})" if context else "Bot API error"

    parameters = payload.get("parameters")
    retry_after: int | None = None
    parameter_details: list[str] = []
    if isinstance(parameters, dict):
        retry_after = parse_retry_after(parameters.get("retry_after"))
        migrate_to_chat_id = parameters.get("migrate_to_chat_id")
        if isinstance(migrate_to_chat_id, int) and not isinstance(migrate_to_chat_id, bool):
            parameter_details.append(f"migrate_to_chat_id={migrate_to_chat_id}")

    if retry_after is None:
        retry_after = parse_retry_after(retry_after_header)
    if retry_after is not None:
        parameter_details.insert(0, f"retry_after={retry_after}s")

    message = f"{label}: {description}"
    if parameter_details:
        message = f"{message} ({', '.join(parameter_details)})"

    status = http_status if http_status is not None else error_code
    if status is not None and is_retryable_status(status) and payload.get("ok") is not False:
        return uncertain_upload_error(
            message,
            retry_after_seconds=retry_after,
        )

    if "FILE_PARTS_INVALID" in description_text.upper():
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = None

        if file_size is not None and file_size > SAFE_UPLOAD_LIMIT_BYTES:
            return NonRetryableUploadError(
                f"{message}; file {file_path} is {format_bytes(file_size)} "
                f"({file_size:,} bytes), exceeding the Telegram Bot limit of "
                f"{format_bytes(SAFE_UPLOAD_LIMIT_BYTES)} "
                f"({SAFE_UPLOAD_LIMIT_BYTES:,} bytes); "
                f"{OVERSIZE_POLICY_HINT} Not retrying"
            )

        size_detail = (
            f"; file size={format_bytes(file_size)} ({file_size:,} bytes)"
            if file_size is not None
            else ""
        )
        return NonRetryableUploadError(
            f"{message}; Telegram rejected the file-part count{size_detail}. "
            "The same upload request will not succeed unchanged; not retrying"
        )

    if status is not None and is_retryable_status(status):
        return RetryableUploadError(message, retry_after_seconds=retry_after)
    if status is None:
        return uncertain_upload_error(
            f"{message}; Bot API returned an error without a valid status code"
        )

    return NonRetryableUploadError(f"{message}; request rejected; not retrying")


def is_retryable_status(status: int) -> bool:
    return status in {408, 429} or 500 <= status < 600


def send_file_with_progress(
    connection: http.client.HTTPConnection,
    upload_file: BinaryIO,
    file_path: Path,
    file_size: int,
) -> None:
    sent = 0
    started_at = time.monotonic()
    last_report_at = 0.0
    progress_is_tty = _stdout_is_tty()
    progress_width = print_progress(sent, file_size, started_at, force=True)

    try:
        while sent < file_size:
            try:
                chunk = upload_file.read(min(CHUNK_SIZE, file_size - sent))
            except OSError as exc:
                raise NonRetryableUploadError(
                    f"failed to read upload file {file_path}: {exc}; not retrying"
                ) from exc
            if not chunk:
                raise NonRetryableUploadError(
                    f"upload file changed size while reading: {file_path}; "
                    f"expected {file_size:,} bytes, reached EOF after {sent:,} bytes; "
                    "not retrying"
                )

            connection.send(chunk)
            sent += len(chunk)
            now = time.monotonic()
            if now - last_report_at >= PROGRESS_INTERVAL_SECONDS or sent >= file_size:
                progress_width = print_progress(
                    sent,
                    file_size,
                    started_at,
                    force=sent >= file_size,
                    previous_width=progress_width,
                )
                last_report_at = now

        try:
            extra = upload_file.read(1)
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to verify upload file size for {file_path}: {exc}; not retrying"
            ) from exc
        if extra:
            raise NonRetryableUploadError(
                f"upload file grew beyond its initial size of {file_size:,} bytes "
                f"while reading: {file_path}; not retrying"
            )
    finally:
        if progress_is_tty:
            print()


def print_progress(
    sent: int,
    total: int,
    started_at: float,
    *,
    force: bool = False,
    previous_width: int = 0,
) -> int:
    is_tty = _stdout_is_tty()
    if not force and not is_tty:
        return previous_width

    elapsed = max(time.monotonic() - started_at, 0.001)
    speed = sent / elapsed
    percent = (sent / total * 100) if total else 100.0
    remaining = max(total - sent, 0)
    eta = remaining / speed if speed > 0 else 0.0
    line = (
        f"progress: {percent:6.2f}% "
        f"({format_bytes(sent)} / {format_bytes(total)}, "
        f"{format_bytes(speed)}/s, eta {format_duration(eta)})"
    )
    if is_tty:
        padding = " " * max(previous_width - len(line), 0)
        print(f"\r{line}{padding}", end="", flush=True)
        return len(line)

    print(line, flush=True)
    return 0


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, OSError):
        return False


def format_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{escape_multipart_value(name)}"\r\n'
        "\r\n"
        f"{value}\r\n"
    ).encode("utf-8")


def multipart_file_header(
    boundary: str, field_name: str, filename: str, content_type: str
) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{escape_multipart_value(field_name)}"; '
        f'filename="{escape_multipart_value(filename)}"\r\n'
        f"Content-Type: {content_type}\r\n"
        "\r\n"
    ).encode("utf-8")


def escape_multipart_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc

    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def require_object(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise AppError(f"config.{key} must be an object")
    return value


def require_non_empty_string(
    raw: dict[str, Any],
    key: str,
    *,
    prefix: str | None = None,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        name = f"{prefix}.{key}" if prefix else f"config.{key}"
        raise AppError(f"{name} must be a non-empty string")
    return value


def require_int(raw: dict[str, Any], key: str, *, prefix: str | None = None) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        name = f"{prefix}.{key}" if prefix else f"config.{key}"
        raise AppError(f"{name} must be an integer")
    return value


def optional_int(
    raw: dict[str, Any],
    key: str,
    *,
    prefix: str | None = None,
) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        name = f"{prefix}.{key}" if prefix else f"config.{key}"
        raise AppError(f"{name} must be an integer or null")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
