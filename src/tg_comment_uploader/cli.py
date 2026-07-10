from __future__ import annotations

import argparse
import errno
import http.client
import json
import mimetypes
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.5
UPLOAD_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_PROFILE = "default"
DEFAULT_RETRIES = 5
MAX_LOCAL_BOT_API_UPLOAD_MIB = 2000
MAX_LOCAL_BOT_API_UPLOAD_BYTES = MAX_LOCAL_BOT_API_UPLOAD_MIB * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60
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
    """Transient upload failure that may succeed when attempted again."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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

    server.work_dir.mkdir(parents=True, exist_ok=True)

    command = [
        binary,
        "--api-id",
        str(config.bot.api_id),
        "--api-hash",
        config.bot.api_hash,
        "--local",
        "--http-ip-address",
        server.host,
        "--http-port",
        str(server.port),
    ]

    print(f"starting {server.binary} on http://{server.host}:{server.port}")
    print(f"working directory: {server.work_dir}")
    return subprocess.run(command, cwd=server.work_dir, check=False).returncode


def run_upload(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    profile = get_profile(config, args.profile)
    files = validate_upload_paths(args.paths)

    print_upload_plan(args.profile, profile, files)

    for index, path in enumerate(files, start=1):
        caption = render_caption(profile.caption, path)
        print(f"[{index}/{len(files)}] uploading: {path}")
        result = upload_with_retries(config, profile, path, caption, retries=args.retries)
        message_id = result.get("message_id", "unknown")
        print(f"[{index}/{len(files)}] uploaded message_id={message_id}")

    return 0


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise AppError(f"config file does not exist: {path}")
    if not path.is_file():
        raise AppError(f"config path is not a file: {path}")

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


def validate_upload_paths(paths: list[str]) -> list[Path]:
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


def validate_upload_file_size(path: Path, file_size: int) -> None:
    if file_size == 0:
        raise NonRetryableUploadError(f"upload file is empty: {path}; no upload was attempted")
    if file_size <= MAX_LOCAL_BOT_API_UPLOAD_BYTES:
        return

    raise NonRetryableUploadError(
        f"upload file is too large for Telegram local Bot API: {path}; "
        f"size={format_bytes(file_size)} ({file_size:,} bytes), "
        f"limit={MAX_LOCAL_BOT_API_UPLOAD_MIB} MiB "
        f"({MAX_LOCAL_BOT_API_UPLOAD_BYTES:,} bytes). "
        "Split or re-encode the file before uploading; no upload was attempted."
    )


def print_upload_plan(profile_name: str, profile: ProfileConfig, files: list[Path]) -> None:
    print(f"profile: {profile_name}")
    print(f"chat_id: {profile.chat_id}")
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
    values = {
        "name": path.name,
        "stem": path.stem,
        "suffix": path.suffix,
        "parent": path.parent.name,
        "path": str(path),
    }
    try:
        return template.format(**values)
    except KeyError as exc:
        key = exc.args[0]
        raise AppError(f"unknown caption placeholder: {{{key}}}") from exc


def validate_caption_template(template: str, profile_name: str) -> None:
    allowed = {"name", "stem", "suffix", "parent", "path"}
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is None:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        if root not in allowed:
            raise AppError(
                f"profiles.{profile_name}.caption contains unknown placeholder {{{field_name}}}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )


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
    if not isinstance(result, dict):
        raise NonRetryableUploadError(
            f"upload outcome is uncertain for {path}: "
            "Bot API reported success without a result object; not retrying to avoid duplicates"
        )
    return result


def upload_with_retries(
    config: AppConfig,
    profile: ProfileConfig,
    path: Path,
    caption: str,
    *,
    retries: int,
) -> dict[str, Any]:
    attempts = retries + 1
    last_error: RetryableUploadError | None = None

    for attempt in range(1, attempts + 1):
        try:
            if attempts > 1:
                print(f"attempt {attempt}/{attempts}: {path}")
            return send_video(config, profile, path, caption)
        except RetryableUploadError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(f"attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
            if exc.retry_after_seconds is not None and exc.retry_after_seconds > 0:
                print(
                    f"waiting {exc.retry_after_seconds}s before retrying",
                    file=sys.stderr,
                )
                time.sleep(exc.retry_after_seconds)

    assert last_error is not None
    raise AppError(
        f"upload failed after {attempts} attempts for {path}: {last_error}"
    ) from last_error


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
    file_size = get_upload_file_size(file_path)
    validate_upload_file_size(file_path, file_size)
    closing = f"\r\n--{boundary}--\r\n".encode()
    content_length = (
        sum(len(part) for part in field_parts) + len(file_header) + file_size + len(closing)
    )

    connection = http.client.HTTPConnection(host, port, timeout=UPLOAD_TIMEOUT_SECONDS)
    request_body_sent = False
    response: http.client.HTTPResponse | None = None
    body: bytes | None = None
    try:
        connection.putrequest("POST", f"/bot{token}/{method}")
        connection.putheader("Host", f"{host}:{port}")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()

        for part in field_parts:
            connection.send(part)
        connection.send(file_header)
        send_file_with_progress(connection, file_path, file_size)
        connection.send(closing)
        request_body_sent = True

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
        payload = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-UTF-8 response with HTTP {response.status}",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc
    except json.JSONDecodeError as exc:
        snippet = body[:500].decode("utf-8", errors="replace")
        raise response_protocol_error(
            response.status,
            f"Bot API returned invalid JSON with HTTP {response.status}: {snippet}",
            retry_after_header=response.getheader("Retry-After"),
        ) from exc

    if not isinstance(payload, dict):
        raise response_protocol_error(
            response.status,
            f"Bot API returned a non-object JSON response with HTTP {response.status}",
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


def response_protocol_error(
    http_status: int,
    message: str,
    *,
    retry_after_header: str | None = None,
) -> AppError:
    if is_retryable_status(http_status):
        return RetryableUploadError(
            message,
            retry_after_seconds=parse_retry_after(retry_after_header),
        )
    return NonRetryableUploadError(f"{message}; not retrying")


def make_transport_error(
    host: str,
    port: int,
    error: OSError | http.client.HTTPException,
    *,
    request_body_sent: bool,
    http_status: int | None,
    retry_after_header: str | None,
) -> AppError:
    if http_status is not None:
        message = f"Bot API response failed with HTTP {http_status}: {error}"
        if is_retryable_status(http_status):
            return RetryableUploadError(
                message,
                retry_after_seconds=parse_retry_after(retry_after_header),
            )
        if 200 <= http_status < 300:
            return NonRetryableUploadError(
                f"{message}; upload outcome is unknown; not retrying to avoid a duplicate message"
            )
        return NonRetryableUploadError(f"{message}; request rejected; not retrying")

    if request_body_sent:
        return NonRetryableUploadError(
            f"upload request body was fully sent to {host}:{port}, but no response was received: "
            f"{error}; upload outcome is unknown; not retrying to avoid a duplicate message"
        )

    if isinstance(error, ConnectionRefusedError):
        return NonRetryableUploadError(
            f"local Bot API server is not running at {host}:{port}. "
            "Start it first in another terminal with: just server; not retrying"
        )

    if isinstance(error, socket.gaierror):
        return NonRetryableUploadError(
            f"failed to resolve local Bot API server host {host!r}: {error}; not retrying"
        )

    if is_transient_transport_error(error):
        return RetryableUploadError(
            f"transient error while sending to local Bot API server at {host}:{port}: {error}"
        )

    return NonRetryableUploadError(
        f"non-transient error while calling local Bot API server at {host}:{port}: "
        f"{error}; not retrying"
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
    description = description_raw if isinstance(description_raw, str) else repr(description_raw)
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

    if "FILE_PARTS_INVALID" in description.upper():
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = None

        if file_size is not None and file_size > MAX_LOCAL_BOT_API_UPLOAD_BYTES:
            return NonRetryableUploadError(
                f"{message}; file {file_path} is {format_bytes(file_size)} "
                f"({file_size:,} bytes), exceeding the Telegram Bot limit of "
                f"{MAX_LOCAL_BOT_API_UPLOAD_MIB} MiB "
                f"({MAX_LOCAL_BOT_API_UPLOAD_BYTES:,} bytes); not retrying"
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

    status = http_status if http_status is not None else error_code
    if status is not None and is_retryable_status(status):
        return RetryableUploadError(message, retry_after_seconds=retry_after)

    return NonRetryableUploadError(f"{message}; request rejected; not retrying")


def is_retryable_status(status: int) -> bool:
    return status in {408, 429} or 500 <= status < 600


def send_file_with_progress(
    connection: http.client.HTTPConnection,
    file_path: Path,
    file_size: int,
) -> None:
    sent = 0
    started_at = time.monotonic()
    last_report_at = 0.0

    print_progress(sent, file_size, started_at, force=True)
    try:
        file = file_path.open("rb")
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to open upload file {file_path}: {exc}; not retrying"
        ) from exc

    with file:
        while sent < file_size:
            try:
                chunk = file.read(min(CHUNK_SIZE, file_size - sent))
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
                print_progress(sent, file_size, started_at, force=sent >= file_size)
                last_report_at = now

        try:
            extra = file.read(1)
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to verify upload file size for {file_path}: {exc}; not retrying"
            ) from exc
        if extra:
            raise NonRetryableUploadError(
                f"upload file grew beyond its initial size of {file_size:,} bytes "
                f"while reading: {file_path}; not retrying"
            )

    print()


def print_progress(sent: int, total: int, started_at: float, *, force: bool = False) -> None:
    if not force and not sys.stdout.isatty():
        return

    elapsed = max(time.monotonic() - started_at, 0.001)
    speed = sent / elapsed
    percent = (sent / total * 100) if total else 100.0
    remaining = max(total - sent, 0)
    eta = remaining / speed if speed > 0 else 0.0

    print(
        "\r"
        f"progress: {percent:6.2f}% "
        f"({format_bytes(sent)} / {format_bytes(total)}, "
        f"{format_bytes(speed)}/s, eta {format_duration(eta)})",
        end="",
        flush=True,
    )


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
