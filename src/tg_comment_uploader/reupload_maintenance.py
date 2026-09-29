"""Queue abandonment and download cleanup, applied before starting the listener."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from .errors import AppError
from .reupload_download import cleanup_job
from .reupload_queue import ReuploadQueue


def discard_pending(queue: ReuploadQueue) -> None:
    pending = queue.discard_pending(now=int(time.time()))
    print(f"permanently abandoned {len(pending)} pending task(s); confirmed records retained")
    cutoff = datetime.fromtimestamp(queue.state.discarded_through, UTC).isoformat()
    print(f"old forwards through {cutoff} will be ignored, including offline updates")
    uncertain = sum(job.status == "sending" for job in pending)
    if uncertain:
        print(
            f"{uncertain} abandoned task(s) had uncertain delivery; Telegram copies may already "
            "exist, check the chat before forwarding them again"
        )


def delete_downloads(queue: ReuploadQueue) -> None:
    if queue.state.download_migration is not None:
        raise AppError(
            "download migration is unfinished; finish the migration before retrying --delete-downloads"
        )
    failures: list[str] = []
    pending = sum(not job.terminal for job in queue.state.jobs)
    for job in queue.state.jobs:
        if not job.terminal:
            continue
        try:
            cleanup_job(queue, job)
        except (OSError, AppError) as exc:
            failures.append(job.key)
            print(f"could not clean {job.key} ({type(exc).__name__})")
    if failures:
        raise AppError(
            f"queue state retained; cleanup failed for {len(failures)} task(s), "
            "rerun with --delete-downloads to retry cleanup"
        )
    print("downloaded files for finished tasks removed; queue and deduplication records retained")
    if pending:
        print(
            f"downloads for {pending} unfinished task(s) retained for resume; "
            "combine --discard-pending --delete-downloads to abandon and remove them"
        )
