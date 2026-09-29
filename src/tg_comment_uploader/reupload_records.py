"""Validated, backwards-compatible records for the durable reupload queue."""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from telethon import utils

from .reupload_message import decode_message

PendingStatus = Literal["queued", "downloading", "ready", "sending"]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class DownloadedFile(Record):
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ThumbnailFile(DownloadedFile):
    source: Literal["original", "generated"]


class DownloadReference(Record):
    access_hash: int = Field(ge=-(2**63), lt=2**63)
    file_reference: str = Field(min_length=1)

    @field_validator("file_reference")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("invalid cached media reference") from exc
        if not decoded:
            raise ValueError("empty cached media reference")
        return value


class QueuedMessage(Record):
    message_id: int = Field(gt=0)
    snapshot: str
    random_id: int = Field(gt=0, lt=2**63)
    downloaded: DownloadedFile | None = None
    download_reference: DownloadReference | None = None
    thumbnail: ThumbnailFile | None = None


class Job(Record):
    key: str
    grouped_id: int | None
    messages: list[QueuedMessage] = Field(min_length=1, max_length=10)
    received_at: float
    status: PendingStatus | Literal["confirmed", "duplicate", "abandoned"] = "queued"
    output_ids: list[int] = Field(default_factory=list)
    duplicate_of: str | None = None
    abandoned_from: PendingStatus | None = None
    cleanup_pending: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in {"confirmed", "duplicate", "abandoned"}

    @property
    def first_id(self) -> int:
        return self.messages[0].message_id

    @property
    def random_ids(self) -> tuple[int, ...]:
        return tuple(m.random_id for m in self.messages)


class QueueState(Record):
    version: Literal[1] = 1
    owner: str
    chat_id: int
    download_root: str
    download_migration: str | None = None
    started_through: int = Field(default=0, ge=0)
    discarded_through: int = Field(default=0, ge=0)
    problem: str | None = None
    jobs: list[Job] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_integrity(self) -> QueueState:
        inputs: set[int] = set()
        random_ids: set[int] = set()
        outputs: set[int] = set()
        keys: set[str] = set()
        if [j.first_id for j in self.jobs] != sorted(j.first_id for j in self.jobs):
            raise ValueError("jobs are out of order")
        for job in self.jobs:
            if job.key in keys or job.key != job_key(job.first_id, job.grouped_id):
                raise ValueError("invalid job identity")
            keys.add(job.key)
            ids = [m.message_id for m in job.messages]
            if ids != sorted(ids) or (job.grouped_id is None and len(ids) != 1):
                raise ValueError("invalid input order")
            if (job.status == "duplicate") != (job.duplicate_of is not None):
                raise ValueError("invalid duplicate record")
            if (job.status == "abandoned") != (job.abandoned_from is not None):
                raise ValueError("invalid abandoned record")
            if job.cleanup_pending and job.status != "confirmed":
                raise ValueError("automatic cleanup requires confirmed delivery")
            if job.status == "confirmed":
                if len(job.output_ids) != len(ids):
                    raise ValueError("incomplete confirmation")
            elif job.output_ids:
                raise ValueError("unconfirmed output IDs")
            for output_id in job.output_ids:
                if type(output_id) is not int or output_id <= 0 or output_id in outputs:
                    raise ValueError("invalid output identity")
                outputs.add(output_id)
            for item in job.messages:
                if item.message_id in inputs or item.random_id in random_ids:
                    raise ValueError("duplicate input or random ID")
                inputs.add(item.message_id)
                random_ids.add(item.random_id)
                message = decode_message(item.snapshot)
                if (
                    message.id != item.message_id
                    or message.grouped_id != job.grouped_id
                    or utils.get_peer_id(message.peer_id) != self.chat_id
                ):
                    raise ValueError("snapshot identity does not match queue")
        if inputs & outputs:
            raise ValueError("input and output IDs overlap")
        for job in self.jobs:
            if job.duplicate_of is not None and (
                job.duplicate_of not in keys or job.duplicate_of == job.key
            ):
                raise ValueError("invalid duplicate target")
        return self


def job_key(message_id: int, grouped_id: int | None) -> str:
    return f"album-{grouped_id}" if grouped_id is not None else f"message-{message_id}"
