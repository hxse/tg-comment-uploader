"""Shared command wiring, lock and reconnect policy for download and reupload."""

from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path
from datetime import UTC, datetime

from .config import DEFAULT_CONFIG, load_config
from .errors import AppError, RetryableUploadError
from .locking import UploadLockError, find_project_root, upload_instance_lock
from .mtproto_sender import MtprotoSender
from .retry_policy import retry_delay_seconds
from .reupload_maintenance import delete_downloads, discard_pending
from .reupload_paths import resolve_download_root
from .reupload_queue import ReuploadQueue
from .reupload_relocate import relocate_downloads
from .reupload_service import RECONNECT_STABLE_SECONDS, ReuploadService
from .upload_state import get_mtproto_paths


def add_reupload_parser(subparsers: argparse._SubParsersAction) -> None:
    _add_listener_parser(subparsers, "reupload", download_only=False)
    _add_listener_parser(subparsers, "download", download_only=True)


def _add_listener_parser(
    subparsers: argparse._SubParsersAction, name: str, *, download_only: bool
) -> None:
    help_text = (
        "download forwards and keep local files for optional later reupload"
        if download_only
        else "download forwards and upload back to the same chat"
    )
    parser = subparsers.add_parser(
        name, help=help_text, description=help_text + "; resumes unfinished jobs by default"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--download-dir",
        type=Path,
        metavar="PATH",
        help="override download directory (default: system Downloads/tg-comment-uploader/reupload)",
    )
    parser.add_argument(
        "--status", action="store_true", help="show local queue counts without login"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="start fresh: permanently abandon unfinished jobs and old offline forwards; keep files",
    )
    if not download_only:
        parser.add_argument(
            "--delete-downloads",
            action="store_true",
            help="clean finished task downloads before listening; unfinished task files are retained",
        )
        parser.add_argument(
            "--keep-downloads",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="keep files after upload; --no-keep-downloads removes them (default: config)",
        )
    else:
        parser.set_defaults(keep_downloads=True, delete_downloads=False)
    parser.set_defaults(func=run_reupload, download_only=download_only)


def run_reupload(args: argparse.Namespace) -> int:
    from .cli import termination_as_interrupt

    try:
        with termination_as_interrupt(), upload_instance_lock():
            return run_reupload_locked(args)
    except UploadLockError as exc:
        raise AppError(str(exc)) from exc


def run_reupload_locked(args: argparse.Namespace) -> int:
    if args.status and (args.fresh or args.delete_downloads):
        raise AppError("--status cannot be combined with --fresh or --delete-downloads")
    config = load_config(args.config)
    options = config.reupload
    if options is None or options.chat_id is None:
        raise AppError(
            "set reupload.chat_id in the config to your backup channel or private chat ID"
        )
    download_root = resolve_download_root(
        args.download_dir, options.download_dir, project_root=find_project_root()
    )
    paths = get_mtproto_paths(
        args.config, config.bot.token, api_id=config.bot.api_id, api_hash=config.bot.api_hash
    )
    owner = f"{paths.owner.config_fingerprint}:bot{paths.owner.bot_id}"
    namespace = hashlib.sha256(f"{owner}:{options.chat_id}".encode()).hexdigest()[:32]
    queue = ReuploadQueue(
        paths.pending_path.parent / "reupload" / namespace / "queue-v1.json",
        owner=owner,
        chat_id=int(options.chat_id),
        download_root=download_root,
        allow_download_root_change=True,
    )
    confirmed = sum(j.status == "confirmed" for j in queue.state.jobs)
    pending = sum(not j.terminal for j in queue.state.jobs)
    legacy_skipped = sum(j.status == "duplicate" for j in queue.state.jobs)
    abandoned = sum(j.status == "abandoned" for j in queue.state.jobs)
    if args.download_only:
        downloaded = sum(j.status == "ready" for j in queue.state.jobs)
        awaiting_confirmation = sum(j.status == "sending" for j in queue.state.jobs)
        pending_downloads = sum(j.needs_download for j in queue.state.jobs)
        print(
            f"download queue: {downloaded} downloaded, {pending_downloads} pending download, "
            f"{awaiting_confirmation} awaiting upload confirmation; "
            f"{confirmed} uploaded, {legacy_skipped} legacy skipped, {abandoned} abandoned"
        )
    else:
        print(
            f"reupload queue: {confirmed} confirmed, {pending} pending, "
            f"{legacy_skipped} legacy skipped, {abandoned} abandoned"
        )
    print(f"downloads: {download_root}")
    print(f"state: {queue.path}")
    if args.status:
        if legacy_skipped:
            print("legacy skipped tasks will be restored on startup, unless previously discarded")
        if queue.state.download_root != str(download_root):
            print(f"stored downloads: {queue.state.download_root}; migration pending until startup")
        if queue.state.download_migration:
            print(f"unfinished download migration: {queue.state.download_migration}")
        if queue.state.discarded_through:
            cutoff = datetime.fromtimestamp(queue.state.discarded_through, UTC).isoformat()
            print(f"ignoring old forwards through {cutoff}")
        if queue.state.problem:
            print(f"stopped: {queue.state.problem}")
        return 0
    restored, discarded = queue.restore_legacy_skips()
    if restored or discarded:
        print(f"legacy skipped tasks: {restored} restored, {discarded} kept abandoned")
    # Apply --fresh only once: reconnects must retain work received by this run.
    if args.fresh:
        discard_pending(queue)
    if args.delete_downloads:
        print(f"cleaning stored downloads: {queue.state.download_root}")
        delete_downloads(queue)
    relocate_downloads(queue, download_root)
    queue.check_problem()
    failures = 0
    while True:
        service = ReuploadService(
            queue,
            bot_id=paths.owner.bot_id,
            retries=options.retries,
            keep_downloads=(
                options.keep_downloads if args.keep_downloads is None else args.keep_downloads
            ),
            download_only=args.download_only,
        )
        try:
            with MtprotoSender(
                api_id=config.bot.api_id,
                api_hash=config.bot.api_hash,
                bot_token=config.bot.token,
                session_path=paths.session_path,
                chat_id=options.chat_id,
                reply_message_id=None,
                supports_streaming=True,
                client_factory=service.client_factory,
                final_request_status=service.progress.final_request_status,
            ) as sender:
                sender.run_service(service.run(sender))
            return 0
        except RetryableUploadError as exc:
            if service.connected_seconds >= RECONNECT_STABLE_SECONDS:
                if failures:
                    print("listener recovered and stayed stable; reconnect retry count reset")
                failures = 0
            failures += 1
            if failures > options.retries:
                raise AppError(
                    f"listener failed after {failures} consecutive attempts; "
                    f"rerun just {service.command} to resume: {exc}"
                ) from exc
            delay = retry_delay_seconds(exc, failed_attempt=failures)
            print(f"reconnecting in {delay}s; queue and random IDs retained: {exc}", flush=True)
            time.sleep(delay)
