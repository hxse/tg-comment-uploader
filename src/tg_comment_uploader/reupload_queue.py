"""Durable, ordered JSON queue; every input owns one persistent random ID."""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from pydantic import ValidationError
from telethon import types

from .errors import AppError
from .reupload_message import decode_message, encode_message
from .reupload_records import (
    DownloadedFile,
    DownloadReference,
    Job,
    QueuedMessage,
    QueueState,
    ThumbnailFile,
    job_key,
)
from .state_io import read_private_json, write_private_json


class ReuploadQueue:
    def __init__(
        self,
        path: Path,
        *,
        owner: str,
        chat_id: int,
        download_root: Path,
        allow_download_root_change: bool = False,
    ) -> None:
        self.path = path
        if path.exists() or path.is_symlink():
            try:
                self.state = QueueState.model_validate(read_private_json(path))
            except (ValidationError, ValueError) as exc:
                raise AppError("invalid reupload queue; refusing to send anything") from exc
            if (
                self.state.owner != owner
                or self.state.chat_id != chat_id
                or (
                    not allow_download_root_change
                    and self.state.download_root != str(download_root)
                )
            ):
                raise AppError(
                    "reupload queue belongs to a different bot, chat or download directory"
                )
        else:
            self.state = QueueState(owner=owner, chat_id=chat_id, download_root=str(download_root))
        self._deferred_keys: set[str] = set()
        self._inputs = {m.message_id for j in self.state.jobs for m in j.messages}
        self._random_ids = {m.random_id for j in self.state.jobs for m in j.messages}

    def download_directory(self, root: Path | None = None) -> Path:
        namespace = hashlib.sha256(f"{self.state.owner}:{self.state.chat_id}".encode()).hexdigest()[
            :24
        ]
        return (root if root is not None else Path(self.state.download_root)) / namespace

    def set_download_location(self, root: Path, *, migration: Path | None = None) -> None:
        self._commit(
            self.state.model_copy(
                update={
                    "download_root": str(root),
                    "download_migration": str(migration) if migration is not None else None,
                }
            )
        )

    def defer_pending(self) -> int:
        """Exclude existing pending jobs for this command without modifying their state."""
        self._deferred_keys = {j.key for j in self.state.jobs if not j.terminal}
        return len(self._deferred_keys)

    def _commit(self, state: QueueState) -> None:
        write_private_json(self.path, state.model_dump(mode="json"), label="reupload queue")
        self.state = state

    def update(self, job: Job, **state_fields: object) -> None:
        jobs = [job if old.key == job.key else old for old in self.state.jobs]
        self._commit(self.state.model_copy(update={"jobs": jobs, **state_fields}))

    def get(self, key: str) -> Job:
        return next(job for job in self.state.jobs if job.key == key)

    def discard_pending(self, *, now: int) -> list[Job]:
        pending = [job for job in self.state.jobs if not job.terminal]
        jobs = [
            job.model_copy(update={"status": "abandoned", "abandoned_from": job.status})
            if not job.terminal
            else job
            for job in self.state.jobs
        ]
        self._commit(
            self.state.model_copy(
                update={
                    "jobs": jobs,
                    "discarded_through": max(now, self.state.discarded_through),
                    "problem": None,
                }
            )
        )
        return pending

    def restore_legacy_skips(self) -> tuple[int, int]:
        """Recover never-sent forwards skipped by the old content-based rule."""
        restored = abandoned = 0
        jobs: list[Job] = []
        for job in self.state.jobs:
            if job.status == "duplicate":
                dates = [decode_message(item.snapshot).date for item in job.messages]
                discarded = bool(self.state.discarded_through) and any(
                    date is None or date.timestamp() <= self.state.discarded_through
                    for date in dates
                )
                job = job.model_copy(
                    update={
                        "status": "abandoned" if discarded else "queued",
                        "abandoned_from": "queued" if discarded else None,
                        "duplicate_of": None,
                    }
                )
                abandoned += int(discarded)
                restored += int(not discarded)
            jobs.append(job)
        if restored or abandoned:
            self._commit(self.state.model_copy(update={"jobs": jobs}))
        return restored, abandoned

    def enqueue(self, message: types.Message, *, now: float | None = None) -> bool:
        if message.id in self._inputs:
            return False
        if self.state.discarded_through and (
            message.date is None or message.date.timestamp() <= self.state.discarded_through
        ):
            return False
        key = job_key(message.id, message.grouped_id)
        if key in self._deferred_keys:
            return False
        old = next((j for j in self.state.jobs if j.key == key), None)
        if old is not None and old.status == "abandoned":
            return False
        problem = self.state.problem
        if message.id <= self.state.started_through or (old and old.status != "queued"):
            problem = f"late input message {message.id}; stopped to avoid incorrect ordering"
        random_id = secrets.randbits(63) or 1
        while random_id in self._random_ids:
            random_id = secrets.randbits(63) or 1
        item = QueuedMessage(
            message_id=message.id, snapshot=encode_message(message), random_id=random_id
        )
        if old and old.status != "queued":
            # Preserve the sealed album and its random-ID-to-member mapping.
            # Keep the late input separately as evidence; it cannot be uploaded.
            self._commit(self.state.model_copy(update={"problem": problem}))
            write_private_json(
                self.path.parent / f"late-{message.id}.json",
                item.model_dump(mode="json"),
                label="late reupload input",
            )
            return True
        members = sorted([*(old.messages if old else []), item], key=lambda m: m.message_id)
        if len(members) > 10:
            raise AppError("received more than 10 members in one Telegram album")
        job = Job(
            key=key,
            grouped_id=message.grouped_id,
            messages=members,
            received_at=time.time() if now is None else now,
        )
        jobs = sorted(
            [j for j in self.state.jobs if j.key != key] + [job], key=lambda j: j.first_id
        )
        self._commit(self.state.model_copy(update={"jobs": jobs, "problem": problem}))
        self._inputs.add(message.id)
        self._random_ids.add(random_id)
        return True

    def check_problem(self) -> None:
        if self.state.problem:
            raise AppError(self.state.problem)

    def next_job(self) -> Job | None:
        self.check_problem()
        return next(
            (j for j in self.state.jobs if not j.terminal and j.key not in self._deferred_keys),
            None,
        )

    def begin(self, job: Job) -> Job:
        self.check_problem()
        if self.next_job() != job:
            raise AppError("cannot start a later job before an earlier input")
        if any(
            job.first_id < other.first_id < job.messages[-1].message_id
            for other in self.state.jobs
            if other.key != job.key
        ):
            raise AppError("album is interleaved with other inputs; cannot preserve both orders")
        if job.grouped_id is not None and len(job.messages) < 2:
            raise AppError(f"album {job.grouped_id} is incomplete; waiting for its other members")
        if job.status == "queued":
            job = job.model_copy(update={"status": "downloading"})
            self.update(
                job, started_through=max(self.state.started_through, job.messages[-1].message_id)
            )
        return job

    def ready(self, key: str) -> None:
        job = self.get(key)
        if job.status != "sending":
            self.update(job.model_copy(update={"status": "ready"}))

    def downloaded(self, key: str, message_id: int, file: DownloadedFile) -> None:
        job = self.get(key)
        messages = [
            m.model_copy(update={"downloaded": file}) if m.message_id == message_id else m
            for m in job.messages
        ]
        self.update(job.model_copy(update={"messages": messages}))

    def cache_download_reference(
        self,
        key: str,
        message_id: int,
        reference: DownloadReference,
    ) -> None:
        job = self.get(key)
        messages = [
            m.model_copy(update={"download_reference": reference})
            if m.message_id == message_id
            else m
            for m in job.messages
        ]
        self.update(job.model_copy(update={"messages": messages}))

    def thumbnail_downloaded(self, key: str, message_id: int, thumbnail: ThumbnailFile) -> None:
        job = self.get(key)
        messages = [
            m.model_copy(update={"thumbnail": thumbnail}) if m.message_id == message_id else m
            for m in job.messages
        ]
        self.update(job.model_copy(update={"messages": messages}))

    def sending(self, key: str) -> None:
        self.check_problem()
        job = self.get(key)
        if self.next_job() != job:
            raise AppError("queue order changed before sending; refusing to overtake an input")
        self.update(job.model_copy(update={"status": "sending"}))

    def confirm(
        self, key: str, output_ids: tuple[int, ...], *, cleanup_pending: bool = False
    ) -> None:
        job = self.get(key)
        used_ids = self._inputs | {
            i for other in self.state.jobs if other.key != key for i in other.output_ids
        }
        if (
            len(output_ids) != len(job.messages)
            or len(set(output_ids)) != len(output_ids)
            or any(type(i) is not int or i <= 0 for i in output_ids)
            or used_ids.intersection(output_ids)
        ):
            raise AppError("invalid Telegram confirmation; preserving pending random IDs")
        self.update(
            job.model_copy(
                update={
                    "status": "confirmed",
                    "output_ids": list(output_ids),
                    "cleanup_pending": cleanup_pending,
                }
            )
        )
