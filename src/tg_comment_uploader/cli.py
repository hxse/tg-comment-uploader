from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

from .config import (
    AppConfig as AppConfig,
)
from .config import (
    BotConfig as BotConfig,
)
from .config import (
    ProfileConfig as ProfileConfig,
)
from .config import (
    load_config as load_config,
)
from .config import (
    validate_caption_template_shape as validate_caption_template_shape,
)
from .errors import AppError, NonRetryableUploadError, RetryableUploadError
from .local_files import fingerprint_file as fingerprint_file
from .locking import UploadLockError, upload_instance_lock
from .media_compress import CompressionProgress
from .media_split import SplitProgress
from .media_workflow import MediaPreparationError, prepare_media
from .mtproto_sender import MtprotoSender
from .retry_policy import retry_delay_seconds as retry_delay_seconds
from .reupload_cli import add_reupload_parser
from .telegram_sender import (
    SAFE_UPLOAD_LIMIT_BYTES,
    TelegramSender,
    UploadItem,
    validate_upload_file_size,
)
from .terminal_progress import TelegramConfirmationWaitIndicator, TerminalProgress
from .terminal_progress import format_bytes as format_bytes
from .upload_contract import (
    MEDIA_GROUP_MAX_ITEMS,
    MEDIA_GROUP_MIN_ITEMS,
    OVERSIZE_POLICIES,
    OversizePolicy,
)
from .upload_state import (
    FileIdentity,
    MtprotoPaths,
    PendingReplacementKind,
    PendingUnit,
    PendingUpload,
    PendingUploadStore,
    PreparationKind,
    SourceIntent,
    UploadIntent,
    UploadUnitSpec,
    get_mtproto_paths,
)

DEFAULT_PROFILE = "default"
DEFAULT_RETRIES = 5
SPLIT_MEDIA_TARGET_BYTES = SAFE_UPLOAD_LIMIT_BYTES * 98 // 100
COMPRESS_MEDIA_TARGET_BYTES = SAFE_UPLOAD_LIMIT_BYTES * 95 // 100

UploadResultT = TypeVar("UploadResultT")
DEFAULT_CONFIG = Path("config/config.json")


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
    add_reupload_parser(subparsers)

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

    pending_status = subparsers.add_parser(
        "pending-status",
        help="show current pending send/idempotency state without private details",
    )
    pending_status.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pending_status.set_defaults(func=run_pending_status)

    pending_discard = subparsers.add_parser(
        "pending-discard",
        help="discard one exact pending operation after checking Telegram",
    )
    pending_discard.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    pending_discard.add_argument("--operation-id", required=True)
    pending_discard.add_argument(
        "--force-foreign-owner",
        action="store_true",
        help="allow discarding state created by another config or bot",
    )
    pending_discard.set_defaults(func=run_pending_discard)

    return parser


def run_upload(args: argparse.Namespace) -> int:
    try:
        with termination_as_interrupt(), upload_instance_lock():
            return run_upload_locked(args)
    except UploadLockError as exc:
        raise AppError(str(exc)) from exc


def run_pending_status(args: argparse.Namespace) -> int:
    try:
        with upload_instance_lock():
            config = load_config(args.config)
            paths, store = create_pending_store(args.config, config.bot)
            pending = store.inspect_any_owner()
    except UploadLockError as exc:
        raise AppError(str(exc)) from exc

    if pending is None:
        print("pending upload: <none>")
        return 0

    print("pending upload:")
    print(f"  schema_version: {pending.schema_version}")
    print(f"  operation_id: {pending.operation_id}")
    print(f"  owner: {'current' if pending.owner == paths.owner else 'foreign'}")
    print(f"  stage: {pending.stage}")
    print(f"  units: {len(pending.units)}")
    print(f"  confirmed_units: {pending.confirmed_unit_count}")
    return 0


def run_pending_discard(args: argparse.Namespace) -> int:
    try:
        with upload_instance_lock():
            config = load_config(args.config)
            _, store = create_pending_store(args.config, config.bot)
            foreign_owner = store.discard(
                args.operation_id,
                force_foreign_owner=args.force_foreign_owner,
            )
    except UploadLockError as exc:
        raise AppError(str(exc)) from exc

    owner_label = "foreign-owner " if foreign_owner else ""
    print(f"discarded {owner_label}pending upload operation {args.operation_id}")
    print("a later upload will create new Telegram random IDs")
    print(
        "warning: if an old sending request actually succeeded, uploading again may create "
        "a duplicate Telegram message or media group",
        file=sys.stderr,
    )
    return 0


def prepared_media_target_bytes(policy: OversizePolicy) -> int:
    if policy == "compress":
        return COMPRESS_MEDIA_TARGET_BYTES
    if policy in {"error", "split"}:
        return SPLIT_MEDIA_TARGET_BYTES
    raise ValueError(f"unsupported oversize policy: {policy}")


def create_pending_store(
    config_path: Path,
    bot: BotConfig,
) -> tuple[MtprotoPaths, PendingUploadStore]:
    paths = get_mtproto_paths(
        config_path,
        bot.token,
        api_id=bot.api_id,
        api_hash=bot.api_hash,
    )
    return paths, PendingUploadStore(paths.pending_path, owner=paths.owner)


def report_pending_replacement(
    previous: PendingUpload,
    kind: PendingReplacementKind,
) -> None:
    if kind == "planned":
        print(
            f"automatically replaced planned-only pending operation "
            f"{previous.operation_id}; no final Telegram send had started"
        )
        return
    if kind == "fully-confirmed":
        print(
            f"automatically cleaned fully confirmed pending operation "
            f"{previous.operation_id}; starting the new command"
        )
        return
    raise ValueError(f"unsupported pending replacement kind: {kind}")


def create_sender(
    config: AppConfig,
    profile: ProfileConfig,
    session_path: Path,
    *,
    final_request_status: Callable[[bool], None],
    peer_resolved: Callable[[int], None],
) -> TelegramSender:
    return MtprotoSender(
        api_id=config.bot.api_id,
        api_hash=config.bot.api_hash,
        bot_token=config.bot.token,
        session_path=session_path,
        chat_id=profile.chat_id,
        reply_message_id=profile.reply_message_id,
        supports_streaming=profile.supports_streaming,
        final_request_status=final_request_status,
        peer_resolved=peer_resolved,
    )


def run_upload_locked(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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
    mtproto_paths, state = create_pending_store(args.config, config.bot)
    # Reject corrupt or unsafe foreign state before hashing multi-gigabyte inputs.
    state.preflight_new_upload()
    sources = fingerprint_sources(
        files,
        captions,
        allow_oversized=oversize_policy != "error",
    )
    intent = UploadIntent(
        profile_name=args.profile,
        chat_id=profile.chat_id,
        reply_message_id=profile.reply_message_id,
        supports_streaming=profile.supports_streaming,
        oversize_policy=oversize_policy,
        sources=sources,
    )
    pending = state.open_or_create(intent, on_replaced=report_pending_replacement)
    operation_id = pending.operation_id
    if pending.units:
        print(
            f"resuming pending operation {operation_id}: "
            f"{pending.confirmed_unit_count}/{len(pending.units)} known unit(s) confirmed"
        )
    else:
        print(f"operation_id: {operation_id}")

    if pending.fully_confirmed:
        try:
            state.complete(expected_unit_count=len(pending.units))
        except AppError as exc:
            print(
                "warning: the previous upload was fully confirmed, but its pending state "
                f"could not be removed: {exc}; nothing was resent",
                file=sys.stderr,
            )
        else:
            print("previous fully confirmed operation cleaned up; nothing was resent")
        return 0

    sender: TelegramSender | None = None
    active_renderer: TerminalProgress | None = None
    confirmation = TelegramConfirmationWaitIndicator()
    expected_unit_count = 0

    def final_request_status(active: bool) -> None:
        if active_renderer is not None:
            active_renderer.finish()
        if active:
            confirmation.start()
        else:
            confirmation.stop()

    def persist_resolved_peer(peer_id: int) -> None:
        state.bind_peer(peer_id)

    def get_sender() -> TelegramSender:
        nonlocal sender
        if sender is None:
            sender = create_sender(
                config,
                profile,
                mtproto_paths.session_path,
                final_request_status=final_request_status,
                peer_resolved=persist_resolved_peer,
            )
        return sender

    try:
        for source_index, (path, caption, source) in enumerate(
            zip(files, captions, sources, strict=True),
            start=1,
        ):
            prefix = f"[{source_index}/{len(files)}]"
            restored_source_units = tuple(
                unit for unit in pending.units if unit.source_index == source_index
            )
            renderer = TerminalProgress()
            active_renderer = renderer
            try:
                renderer.log(f"{prefix} uploading: {path}")
                if source_index in pending.completed_sources:
                    expected_unit_count += len(restored_source_units)
                    renderer.log(
                        f"{prefix} restored {len(restored_source_units)} fully confirmed "
                        "upload unit(s); skipping media preparation and send"
                    )
                    continue
                try:
                    with prepare_media(
                        path,
                        oversize_policy,
                        hard_limit_bytes=SAFE_UPLOAD_LIMIT_BYTES,
                        target_bytes=prepared_media_target_bytes(oversize_policy),
                        expected_source_size=source.file.size,
                        expected_source_sha256=source.file.sha256,
                        progress=lambda message: renderer.log(f"{prefix} {message}"),
                        split_progress=lambda event: route_split_progress(renderer, prefix, event),
                        compression_progress=lambda event: route_compression_progress(
                            renderer, prefix, event
                        ),
                        warning=lambda message: print_preparation_warning(renderer, message),
                    ) as prepared:
                        preparation = prepared_media_kind(
                            path,
                            prepared.paths,
                            oversize_policy,
                        )
                        identities = fingerprint_prepared_files(
                            prepared.paths,
                            source=source.file,
                            source_path=path,
                            renderer=renderer,
                            prefix=prefix,
                        )
                        if prepared.is_media_group:
                            groups = partition_media_groups(prepared.paths)
                            identity_offset = 0
                            for group_index, group in enumerate(groups, start=1):
                                group_identities = identities[
                                    identity_offset : identity_offset + len(group)
                                ]
                                identity_offset += len(group)
                                key = f"source:{source_index}/group:{group_index}"
                                unit = state.register_unit(
                                    UploadUnitSpec(
                                        key=key,
                                        source_index=source_index,
                                        preparation=preparation,
                                        files=group_identities,
                                    )
                                )
                                expected_unit_count += 1
                                if unit.status == "confirmed":
                                    renderer.log(
                                        f"{prefix} restored media group {group_index}/"
                                        f"{len(groups)} message_ids="
                                        f"{','.join(map(str, unit.message_ids))}"
                                    )
                                    continue

                                renderer.log(
                                    f"uploading media group {group_index}/{len(groups)} "
                                    f"({len(group)} videos): {path}"
                                )
                                items = tuple(
                                    UploadItem(
                                        item_path,
                                        caption if item_index == 0 else "",
                                        expected_size=group_identities[item_index].size,
                                        expected_sha256=group_identities[item_index].sha256,
                                    )
                                    for item_index, item_path in enumerate(group)
                                )
                                message_ids = upload_media_group_with_retries(
                                    get_sender(),
                                    items,
                                    unit=unit,
                                    label=f"{path} media group {group_index}/{len(groups)}",
                                    retries=args.retries,
                                    progress_callback=media_group_progress_callback(
                                        renderer,
                                        prefix,
                                        len(group),
                                    ),
                                    before_final_request=lambda key=key: mark_unit_sending(
                                        state, key
                                    ),
                                )
                                state.mark_confirmed(key, message_ids)
                                renderer.log(
                                    f"{prefix} uploaded message_ids="
                                    f"{','.join(map(str, message_ids))}"
                                )
                        else:
                            key = f"source:{source_index}/single"
                            unit = state.register_unit(
                                UploadUnitSpec(
                                    key=key,
                                    source_index=source_index,
                                    preparation=preparation,
                                    files=identities,
                                )
                            )
                            expected_unit_count += 1
                            if unit.status == "confirmed":
                                renderer.log(f"{prefix} restored message_id={unit.message_ids[0]}")
                                state.mark_source_completed(source_index)
                                continue

                            message_id = upload_with_retries(
                                get_sender(),
                                UploadItem(
                                    prepared.paths[0],
                                    caption,
                                    expected_size=identities[0].size,
                                    expected_sha256=identities[0].sha256,
                                ),
                                unit=unit,
                                label=str(path),
                                retries=args.retries,
                                progress_callback=upload_progress_callback(
                                    renderer,
                                    f"{prefix} MTProto upload",
                                ),
                                before_final_request=lambda key=key: mark_unit_sending(state, key),
                            )
                            state.mark_confirmed(key, (message_id,))
                            renderer.log(f"{prefix} uploaded message_id={message_id}")
                        state.mark_source_completed(source_index)
                except MediaPreparationError as exc:
                    raise NonRetryableUploadError(str(exc)) from exc
            finally:
                renderer.finish()
                active_renderer = None

        confirmation.stop()
        close_sender_safely(sender)
        sender = None
        try:
            state.complete(expected_unit_count=expected_unit_count)
        except AppError as exc:
            print(
                f"warning: every upload was confirmed, but pending state cleanup failed: {exc}; "
                "the confirmed operation will be resumed without resending next time",
                file=sys.stderr,
            )
        return 0
    except BaseException:
        confirmation.stop()
        close_sender_safely(sender)
        cleanup_empty_pending_state(state, operation_id)
        raise


def fingerprint_sources(
    files: Sequence[Path],
    captions: Sequence[str],
    *,
    allow_oversized: bool,
) -> tuple[SourceIntent, ...]:
    sources: list[SourceIntent] = []
    for index, (path, caption) in enumerate(zip(files, captions, strict=True), start=1):
        renderer = TerminalProgress()
        try:
            renderer.log(f"[{index}/{len(files)}] fingerprinting source for safe recovery: {path}")
            size, digest = fingerprint_file(
                path,
                allow_oversized=allow_oversized,
                progress=lambda sent, total: renderer.update(
                    f"[{index}/{len(files)}] fingerprinting source ({format_bytes(total)})",
                    sent / total if total else 1.0,
                ),
            )
        finally:
            renderer.finish()
        sources.append(
            SourceIntent(
                file=FileIdentity(path=str(path), size=size, sha256=digest),
                caption=caption,
            )
        )
    return tuple(sources)


def fingerprint_prepared_files(
    paths: Sequence[Path],
    *,
    source: FileIdentity,
    source_path: Path,
    renderer: TerminalProgress,
    prefix: str,
) -> tuple[FileIdentity, ...]:
    identities: list[FileIdentity] = []
    for item_index, path in enumerate(paths, start=1):
        if len(paths) == 1 and same_path(path, source_path):
            identities.append(FileIdentity(path=str(path), size=source.size, sha256=source.sha256))
            continue
        size, digest = fingerprint_file(
            path,
            progress=lambda sent, total, item_index=item_index: renderer.update(
                f"{prefix} fingerprinting prepared file {item_index}/{len(paths)} "
                f"({format_bytes(total)})",
                sent / total if total else 1.0,
            ),
        )
        identities.append(FileIdentity(path=str(path), size=size, sha256=digest))
    return tuple(identities)


def same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve(strict=True) == second.resolve(strict=True)
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to resolve prepared upload path: {exc}; not retrying"
        ) from exc


def prepared_media_kind(
    source: Path,
    prepared_paths: Sequence[Path],
    policy: OversizePolicy,
) -> PreparationKind:
    if len(prepared_paths) == 1 and same_path(prepared_paths[0], source):
        return "original"
    if policy == "split":
        return "split"
    if policy == "compress":
        return "compress"
    raise NonRetryableUploadError("oversized media was prepared under the error policy")


def upload_progress_callback(
    renderer: TerminalProgress,
    label: str,
) -> Callable[[int, int], None]:
    def report(sent: int, total: int) -> None:
        renderer.update(
            f"{label} ({format_bytes(total)})",
            sent / total if total > 0 else 1.0,
        )

    return report


def media_group_progress_callback(
    renderer: TerminalProgress,
    prefix: str,
    item_count: int,
) -> Callable[[int, int, int], None]:
    def report(item_index: int, sent: int, total: int) -> None:
        renderer.update(
            f"{prefix} MTProto upload item {item_index + 1}/{item_count} ({format_bytes(total)})",
            sent / total if total > 0 else 1.0,
        )

    return report


def close_sender_safely(sender: TelegramSender | None) -> None:
    if sender is None:
        return
    try:
        sender.close()
    except AppError as exc:
        print(
            f"warning: failed to close the Telegram session cleanly: {exc}; "
            "confirmed upload results are unchanged",
            file=sys.stderr,
        )


def cleanup_empty_pending_state(
    state: PendingUploadStore,
    operation_id: str,
) -> None:
    try:
        pending = state.inspect()
        if pending is not None and pending.operation_id == operation_id and not pending.units:
            state.complete(expected_unit_count=0)
    except AppError as exc:
        print(
            f"warning: failed to clean an empty pending operation: {exc}",
            file=sys.stderr,
        )


def mark_unit_sending(state: PendingUploadStore, key: str) -> None:
    state.mark_sending(key)


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


def partition_media_groups(paths: Sequence[Path]) -> tuple[tuple[Path, ...], ...]:
    if len(paths) < MEDIA_GROUP_MIN_ITEMS:
        raise ValueError(
            f"at least {MEDIA_GROUP_MIN_ITEMS} paths are required for media-group partitioning"
        )

    group_count = (len(paths) + MEDIA_GROUP_MAX_ITEMS - 1) // MEDIA_GROUP_MAX_ITEMS
    base_size, larger_group_count = divmod(len(paths), group_count)
    if (
        base_size < MEDIA_GROUP_MIN_ITEMS
        or base_size + (1 if larger_group_count else 0) > MEDIA_GROUP_MAX_ITEMS
    ):
        raise ValueError("cannot partition paths into valid Telegram media groups")

    groups: list[tuple[Path, ...]] = []
    offset = 0
    for group_index in range(group_count):
        group_size = base_size + (1 if group_index < larger_group_count else 0)
        groups.append(tuple(paths[offset : offset + group_size]))
        offset += group_size
    return tuple(groups)


def upload_media_group_with_retries(
    sender: TelegramSender,
    items: Sequence[UploadItem],
    *,
    unit: PendingUnit,
    label: str,
    retries: int,
    progress_callback: Callable[[int, int, int], None] | None = None,
    before_final_request: Callable[[], None] | None = None,
) -> tuple[int, ...]:
    return retry_upload(
        lambda: sender.send_media_group(
            items,
            random_ids=unit.random_ids,
            progress_callback=progress_callback,
            before_final_request=before_final_request,
        ),
        label=label,
        retries=retries,
    )


def upload_with_retries(
    sender: TelegramSender,
    item: UploadItem,
    *,
    unit: PendingUnit,
    label: str,
    retries: int,
    progress_callback: Callable[[int, int], None] | None = None,
    before_final_request: Callable[[], None] | None = None,
) -> int:
    if len(unit.random_ids) != 1:
        raise AppError("single-video pending unit must contain exactly one random ID")
    return retry_upload(
        lambda: sender.send_video(
            item,
            random_id=unit.random_ids[0],
            progress_callback=progress_callback,
            before_final_request=before_final_request,
        ),
        label=label,
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
                    "the previous final send is not yet confirmed; retrying with the same "
                    "persisted Telegram random ID(s), so this attempt does not create a new "
                    "logical message or media group",
                    file=sys.stderr,
                )
            delay = retry_delay_seconds(exc, failed_attempt=attempt)
            delay_source = (
                "Telegram retry_after"
                if exc.retry_after_seconds is not None
                else "exponential backoff"
            )
            print(f"waiting {delay}s before retrying ({delay_source})", file=sys.stderr)
            time.sleep(delay)

    assert last_error is not None
    recovery_hint = ""
    if last_error.outcome_uncertain:
        recovery_hint = (
            "; the final send remains unconfirmed in pending state; rerun the same command "
            "to resume with the same persisted Telegram random ID(s)"
        )
    raise AppError(
        f"upload failed after {attempts} attempts for {label}: {last_error}{recovery_hint}"
    ) from last_error


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc

    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
