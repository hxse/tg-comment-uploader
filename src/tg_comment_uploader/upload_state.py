"""Persistent MTProto session paths and crash-safe pending upload state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .errors import AppError
from .locking import find_project_root
from .strict_json import StrictJsonError, load_strict_json
from .telegram_sender import TELEGRAM_INT32_MAX, parse_bot_id
from .upload_contract import OVERSIZE_POLICIES

PENDING_UPLOAD_RELATIVE_PATH = Path(".local/tg-comment-uploader/mtproto/pending-upload-v1.json")
SESSION_DIRECTORY_RELATIVE_PATH = Path(".local/tg-comment-uploader/mtproto/sessions")
PENDING_SCHEMA_VERSION = 1
DEFAULT_MEDIA_ALGORITHM_VERSION = "2"
UNIT_STATUSES = {"planned", "sending", "confirmed"}
PREPARATION_KINDS = {"original", "split", "compress"}
SHA256_HEX_LENGTH = 64

_PENDING_KEYS = {
    "schema_version",
    "operation_id",
    "owner",
    "intent",
    "resolved_peer_id",
    "units",
    "completed_sources",
    "completion_ready",
    "created_at",
    "updated_at",
}
_OWNER_KEYS = {"config_fingerprint", "bot_id"}
_INTENT_KEYS = {
    "profile_name",
    "chat_id",
    "reply_message_id",
    "supports_streaming",
    "oversize_policy",
    "media_algorithm_version",
    "sources",
}
_SOURCE_KEYS = {"file", "caption"}
_UNIT_KEYS = {
    "key",
    "source_index",
    "preparation",
    "files",
    "random_ids",
    "status",
    "message_ids",
}
_FILE_KEYS = {"path", "size", "sha256"}

UnitStatus = Literal["planned", "sending", "confirmed"]
PreparationKind = Literal["original", "split", "compress"]
PendingStage = Literal["planned", "sending", "partially-confirmed", "confirmed"]
PendingReplacementKind = Literal["planned", "fully-confirmed"]


@dataclass(frozen=True)
class PendingOwner:
    config_fingerprint: str
    bot_id: int

    def __post_init__(self) -> None:
        if not _is_sha256(self.config_fingerprint):
            raise ValueError("config fingerprint must be lowercase hexadecimal")
        if isinstance(self.bot_id, bool) or not isinstance(self.bot_id, int) or self.bot_id <= 0:
            raise ValueError("bot ID must be a positive integer")


@dataclass(frozen=True)
class MtprotoPaths:
    pending_path: Path
    session_path: Path
    owner: PendingOwner


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or not Path(self.path).is_absolute():
            raise ValueError("file identity path must be a non-empty absolute path")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ValueError("file identity size must be a positive integer")
        if not _is_sha256(self.sha256):
            raise ValueError("file identity sha256 must be lowercase hexadecimal")


@dataclass(frozen=True)
class SourceIntent:
    file: FileIdentity
    caption: str

    def __post_init__(self) -> None:
        if not isinstance(self.caption, str):
            raise ValueError("source caption must be a string")


@dataclass(frozen=True)
class UploadIntent:
    profile_name: str
    chat_id: str
    reply_message_id: int | None
    supports_streaming: bool
    oversize_policy: str
    sources: tuple[SourceIntent, ...]
    media_algorithm_version: str = DEFAULT_MEDIA_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.profile_name, str) or not self.profile_name:
            raise ValueError("profile name must be a non-empty string")
        if not isinstance(self.chat_id, str) or not self.chat_id:
            raise ValueError("chat_id must be a non-empty string")
        if self.reply_message_id is not None and (
            isinstance(self.reply_message_id, bool)
            or not isinstance(self.reply_message_id, int)
            or self.reply_message_id <= 0
            or self.reply_message_id > TELEGRAM_INT32_MAX
        ):
            raise ValueError("reply_message_id must be a positive signed 32-bit integer or None")
        if not isinstance(self.supports_streaming, bool):
            raise ValueError("supports_streaming must be a boolean")
        if self.oversize_policy not in OVERSIZE_POLICIES:
            raise ValueError("oversize_policy is invalid")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("upload intent must contain at least one source")
        if not all(isinstance(source, SourceIntent) for source in self.sources):
            raise ValueError("upload intent contains an invalid source")
        if not isinstance(self.media_algorithm_version, str) or not self.media_algorithm_version:
            raise ValueError("media algorithm version must be a non-empty string")


@dataclass(frozen=True)
class UploadUnitSpec:
    key: str
    source_index: int
    preparation: PreparationKind
    files: tuple[FileIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("upload unit key must be a non-empty string")
        if (
            isinstance(self.source_index, bool)
            or not isinstance(self.source_index, int)
            or self.source_index <= 0
        ):
            raise ValueError("upload unit source_index must be positive")
        if self.preparation not in PREPARATION_KINDS:
            raise ValueError("upload unit preparation is invalid")
        if not isinstance(self.files, tuple) or not self.files:
            raise ValueError("upload unit must contain at least one file")
        if not all(isinstance(file, FileIdentity) for file in self.files):
            raise ValueError("upload unit contains an invalid file identity")


@dataclass(frozen=True)
class PendingUnit:
    key: str
    source_index: int
    preparation: PreparationKind
    files: tuple[FileIdentity, ...]
    random_ids: tuple[int, ...]
    status: UnitStatus
    message_ids: tuple[int, ...]

    @property
    def spec(self) -> UploadUnitSpec:
        return UploadUnitSpec(
            key=self.key,
            source_index=self.source_index,
            preparation=self.preparation,
            files=self.files,
        )


@dataclass(frozen=True)
class PendingUpload:
    schema_version: int
    operation_id: str
    owner: PendingOwner
    intent: UploadIntent
    units: tuple[PendingUnit, ...]
    resolved_peer_id: int | None
    completed_sources: tuple[int, ...]
    completion_ready: bool
    created_at: str
    updated_at: str

    @property
    def confirmed_unit_count(self) -> int:
        return sum(unit.status == "confirmed" for unit in self.units)

    @property
    def fully_confirmed(self) -> bool:
        return (
            bool(self.units)
            and self.completed_sources == tuple(range(1, len(self.intent.sources) + 1))
            and all(unit.status == "confirmed" for unit in self.units)
        )

    @property
    def stage(self) -> PendingStage:
        """Return a conservative human-facing summary of persisted send state."""

        if self.fully_confirmed:
            return "confirmed"
        if any(unit.status == "sending" for unit in self.units):
            return "sending"
        if any(unit.status == "confirmed" for unit in self.units):
            return "partially-confirmed"
        if _is_discardable_planned(self):
            return "planned"
        raise AppError("pending upload state is internally inconsistent")


def get_mtproto_paths(
    config_path: Path,
    bot_token: str,
    *,
    api_id: int,
    api_hash: str,
    project_root: Path | None = None,
) -> MtprotoPaths:
    """Derive private paths without placing credentials or config paths in filenames."""

    try:
        resolved_config = config_path.resolve(strict=True)
    except OSError as exc:
        raise AppError(f"failed to resolve config path {config_path}: {exc}") from exc
    try:
        bot_id = parse_bot_id(bot_token)
    except ValueError as exc:
        raise AppError(f"config.bot.token {exc}") from exc
    config_fingerprint = _fingerprint("config-path", str(resolved_config))
    credential_fingerprint = _fingerprint(
        "telegram-credentials",
        str(api_id),
        api_hash,
        bot_token,
    )
    root = find_project_root() if project_root is None else project_root.resolve()
    sessions_dir = root / SESSION_DIRECTORY_RELATIVE_PATH
    session_name = f"{config_fingerprint[:32]}-bot{bot_id}-{credential_fingerprint[:32]}.session"
    return MtprotoPaths(
        pending_path=root / PENDING_UPLOAD_RELATIVE_PATH,
        session_path=sessions_dir / session_name,
        owner=PendingOwner(config_fingerprint=config_fingerprint, bot_id=bot_id),
    )


class PendingUploadStore:
    """The single pending-operation file protected by the project upload lock."""

    def __init__(self, pending_path: Path, *, owner: PendingOwner) -> None:
        self.path = Path(pending_path)
        self.owner = owner

    def inspect(self) -> PendingUpload | None:
        pending = self._read()
        if pending is not None and pending.owner != self.owner:
            raise AppError("pending upload state belongs to another config or bot")
        return pending

    def inspect_any_owner(self) -> PendingUpload | None:
        """Read strictly validated state for the read-only maintenance command."""

        return self._read()

    def preflight_new_upload(self) -> None:
        """Reject an unsafe foreign checkpoint before expensive source hashing."""

        pending = self._read()
        if (
            pending is not None
            and pending.owner != self.owner
            and not _is_discardable_planned(pending)
        ):
            raise AppError("pending upload state belongs to another config or bot")

    def open_or_create(
        self,
        intent: UploadIntent,
        *,
        on_replaced: Callable[[PendingUpload, PendingReplacementKind], None] | None = None,
    ) -> PendingUpload:
        pending = self._read()
        replacement_kind: PendingReplacementKind | None = None
        if pending is not None:
            same_owner = pending.owner == self.owner
            if same_owner and pending.intent == intent:
                return pending
            if _is_discardable_planned(pending):
                replacement_kind = "planned"
            elif not same_owner:
                raise AppError("pending upload state belongs to another config or bot")
            elif pending.fully_confirmed:
                replacement_kind = "fully-confirmed"
            else:
                raise AppError(
                    "pending upload state does not match this command; inspect it with "
                    "pending-status or discard it explicitly after checking Telegram"
                )

        replacement = self._new_pending(intent)
        # _write() uses os.replace(), so readers observe either the old checkpoint or
        # the complete replacement rather than an unlink/create gap.
        self._write(replacement)
        if pending is not None and replacement_kind is not None and on_replaced is not None:
            on_replaced(pending, replacement_kind)
        return replacement

    def _new_pending(self, intent: UploadIntent) -> PendingUpload:
        now = _now()
        return PendingUpload(
            schema_version=PENDING_SCHEMA_VERSION,
            operation_id=uuid.uuid4().hex,
            owner=self.owner,
            intent=intent,
            units=(),
            resolved_peer_id=None,
            completed_sources=(),
            completion_ready=False,
            created_at=now,
            updated_at=now,
        )

    def register_unit(self, spec: UploadUnitSpec) -> PendingUnit:
        pending = self._require_pending()
        if spec.source_index > len(pending.intent.sources):
            raise AppError(f"upload unit {spec.key!r} refers to an unknown source")
        for unit in pending.units:
            if unit.key != spec.key:
                continue
            if unit.spec != spec:
                raise AppError(
                    f"prepared media for pending unit {spec.key!r} changed; refusing to reuse "
                    "its Telegram random ID"
                )
            return unit

        if pending.completion_ready:
            raise AppError("cannot add a send unit to a completed pending upload")
        if spec.source_index in pending.completed_sources:
            raise AppError("cannot add a send unit to a completed upload source")

        used_ids = {random_id for unit in pending.units for random_id in unit.random_ids}
        random_ids = tuple(_new_random_id(used_ids) for _ in spec.files)
        unit = PendingUnit(
            key=spec.key,
            source_index=spec.source_index,
            preparation=spec.preparation,
            files=spec.files,
            random_ids=random_ids,
            status="planned",
            message_ids=(),
        )
        updated = replace(
            pending,
            units=(*pending.units, unit),
            updated_at=_now(),
        )
        self._write(updated)
        return unit

    def bind_peer(self, peer_id: int) -> PendingUpload:
        if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
            raise AppError("resolved Telegram peer ID must be a non-zero integer")
        pending = self._require_pending()
        if pending.resolved_peer_id == peer_id:
            return pending
        if pending.resolved_peer_id is not None:
            raise AppError(
                "the configured Telegram destination now resolves to a different peer; "
                "refusing to resume the pending upload"
            )
        updated = replace(pending, resolved_peer_id=peer_id, updated_at=_now())
        self._write(updated)
        return updated

    def mark_sending(self, key: str) -> PendingUnit:
        pending = self._require_pending()
        index, unit = _find_unit(pending, key)
        if unit.status in {"sending", "confirmed"}:
            return unit
        if pending.resolved_peer_id is None:
            raise AppError(
                "cannot mark a pending upload unit as sending before binding its Telegram peer"
            )
        updated_unit = replace(unit, status="sending")
        self._replace_unit(pending, index, updated_unit)
        return updated_unit

    def mark_confirmed(self, key: str, message_ids: tuple[int, ...]) -> PendingUnit:
        pending = self._require_pending()
        index, unit = _find_unit(pending, key)
        validated_ids = _validate_message_ids(message_ids, expected_count=len(unit.random_ids))
        if unit.status == "confirmed":
            if unit.message_ids != validated_ids:
                raise AppError(f"pending unit {key!r} has conflicting confirmed message IDs")
            return unit
        if unit.status != "sending":
            raise AppError(f"pending unit {key!r} was not marked sending before confirmation")
        used_message_ids = {
            message_id
            for other_index, other_unit in enumerate(pending.units)
            if other_index != index
            for message_id in other_unit.message_ids
        }
        if used_message_ids.intersection(validated_ids):
            raise AppError("Telegram message IDs conflict with another confirmed upload unit")
        updated_unit = replace(unit, status="confirmed", message_ids=validated_ids)
        self._replace_unit(pending, index, updated_unit)
        return updated_unit

    def mark_source_completed(self, source_index: int) -> PendingUpload:
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index <= 0:
            raise ValueError("source_index must be a positive integer")
        pending = self._require_pending()
        if source_index > len(pending.intent.sources):
            raise AppError("cannot complete an unknown upload source")
        if source_index in pending.completed_sources:
            return pending
        source_units = tuple(unit for unit in pending.units if unit.source_index == source_index)
        if not source_units or any(unit.status != "confirmed" for unit in source_units):
            raise AppError(
                "cannot complete an upload source while one of its send units is unconfirmed"
            )
        completed_sources = tuple(sorted((*pending.completed_sources, source_index)))
        completion_ready = completed_sources == tuple(
            range(1, len(pending.intent.sources) + 1)
        ) and all(unit.status == "confirmed" for unit in pending.units)
        updated = replace(
            pending,
            completed_sources=completed_sources,
            completion_ready=completion_ready,
            updated_at=_now(),
        )
        self._write(updated)
        return updated

    def complete(self, *, expected_unit_count: int) -> None:
        if (
            isinstance(expected_unit_count, bool)
            or not isinstance(expected_unit_count, int)
            or expected_unit_count < 0
        ):
            raise ValueError("expected_unit_count must be a non-negative integer")
        pending = self._require_pending()
        if len(pending.units) != expected_unit_count:
            raise AppError(
                "cannot complete pending upload: prepared unit count does not match checkpoint"
            )
        if any(unit.status != "confirmed" for unit in pending.units):
            raise AppError("cannot complete pending upload while a send unit is unconfirmed")
        if expected_unit_count == 0:
            if pending.completed_sources or pending.completion_ready:
                raise AppError("cannot remove an inconsistent empty pending upload")
            self._unlink()
            return
        expected_sources = tuple(range(1, len(pending.intent.sources) + 1))
        if pending.completed_sources != expected_sources:
            raise AppError("cannot complete pending upload while a source is unfinished")
        if expected_unit_count > 0 and not pending.completion_ready:
            pending = replace(pending, completion_ready=True, updated_at=_now())
            self._write(pending)
        self._unlink()

    def discard(
        self,
        operation_id: str,
        *,
        force_foreign_owner: bool = False,
    ) -> bool:
        """Discard one exact operation, explicitly opting in for a foreign owner."""

        if not isinstance(force_foreign_owner, bool):
            raise ValueError("force_foreign_owner must be a boolean")
        pending = self._read()
        if pending is None:
            raise AppError("there is no pending upload state")
        foreign_owner = pending.owner != self.owner
        if foreign_owner and not force_foreign_owner:
            raise AppError(
                "pending upload state belongs to another config or bot; pass "
                "--force-foreign-owner with its exact operation ID only after checking Telegram"
            )
        if pending.operation_id != operation_id:
            raise AppError("operation ID does not match the pending upload state")
        self._unlink()
        return foreign_owner

    def _replace_unit(
        self,
        pending: PendingUpload,
        index: int,
        updated_unit: PendingUnit,
    ) -> None:
        units = list(pending.units)
        units[index] = updated_unit
        self._write(replace(pending, units=tuple(units), updated_at=_now()))

    def _require_pending(self) -> PendingUpload:
        pending = self.inspect()
        if pending is None:
            raise AppError("there is no pending upload state")
        return pending

    def _read(self) -> PendingUpload | None:
        try:
            path_status = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AppError(
                "pending upload state is unreadable or corrupt; refusing to send anything"
            ) from exc
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise AppError("pending upload state path is not a regular file")

        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            if os.name == "posix":
                flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode):
                raise OSError("pending upload state changed to a non-regular file")
            if os.name == "posix" and stat.S_IMODE(opened_status.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as input_file:
                descriptor = None
                raw = load_strict_json(input_file)
        except (OSError, UnicodeError, StrictJsonError) as exc:
            raise AppError(
                "pending upload state is unreadable or corrupt; refusing to send anything"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            return _pending_from_json(raw)
        except (AppError, KeyError, TypeError, ValueError) as exc:
            raise AppError(
                "pending upload state has an invalid or unsupported schema; "
                "refusing to send anything"
            ) from exc

    def _write(self, pending: PendingUpload) -> None:
        parent = self.path.parent
        _ensure_private_directory(parent)
        temporary = parent / f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        payload = (
            json.dumps(
                _pending_to_json(pending),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                descriptor = None
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                self.path.chmod(0o600)
            _fsync_directory(parent)
        except OSError as exc:
            raise AppError(f"failed to persist pending upload state: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _unlink(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError as exc:
            raise AppError("pending upload state disappeared unexpectedly") from exc
        except OSError as exc:
            raise AppError(f"failed to remove pending upload state: {exc}") from exc
        try:
            _fsync_directory(self.path.parent)
        except OSError:
            # The pathname is already absent in this process. Treat deletion as
            # successful; after a crash the old, fully recoverable checkpoint may
            # reappear, but reporting failure here would incorrectly invite a rerun.
            pass


def _pending_to_json(pending: PendingUpload) -> dict[str, Any]:
    return {
        "schema_version": pending.schema_version,
        "operation_id": pending.operation_id,
        "owner": {
            "config_fingerprint": pending.owner.config_fingerprint,
            "bot_id": pending.owner.bot_id,
        },
        "intent": _intent_to_json(pending.intent),
        "resolved_peer_id": pending.resolved_peer_id,
        "completed_sources": list(pending.completed_sources),
        "completion_ready": pending.completion_ready,
        "units": [
            {
                "key": unit.key,
                "source_index": unit.source_index,
                "preparation": unit.preparation,
                "files": [_file_to_json(file) for file in unit.files],
                "random_ids": list(unit.random_ids),
                "status": unit.status,
                "message_ids": list(unit.message_ids),
            }
            for unit in pending.units
        ],
        "created_at": pending.created_at,
        "updated_at": pending.updated_at,
    }


def _pending_from_json(raw: Any) -> PendingUpload:
    if not isinstance(raw, dict):
        raise ValueError("unsupported schema")
    _require_exact_keys(raw, _PENDING_KEYS, "pending upload")
    if _integer(raw.get("schema_version")) != PENDING_SCHEMA_VERSION:
        raise ValueError("unsupported schema")
    owner_raw = _object(raw, "owner")
    _require_exact_keys(owner_raw, _OWNER_KEYS, "owner")
    intent_raw = _object(raw, "intent")
    units_raw = raw.get("units")
    completed_sources_raw = raw.get("completed_sources")
    if not isinstance(units_raw, list):
        raise TypeError("units")
    if not isinstance(completed_sources_raw, list):
        raise TypeError("completed_sources")
    owner = PendingOwner(
        config_fingerprint=_sha256_value(owner_raw.get("config_fingerprint")),
        bot_id=_positive_int(owner_raw.get("bot_id")),
    )
    intent = _intent_from_json(intent_raw)
    units = tuple(_unit_from_json(unit) for unit in units_raw)
    if any(unit.source_index > len(intent.sources) for unit in units):
        raise ValueError("unit source index")
    if len({unit.key for unit in units}) != len(units):
        raise ValueError("duplicate unit key")
    all_random_ids = [random_id for unit in units for random_id in unit.random_ids]
    if len(set(all_random_ids)) != len(all_random_ids):
        raise ValueError("duplicate random ID")
    all_message_ids = [message_id for unit in units for message_id in unit.message_ids]
    if len(set(all_message_ids)) != len(all_message_ids):
        raise ValueError("duplicate message ID")
    completed_sources = tuple(_positive_int(value) for value in completed_sources_raw)
    if completed_sources != tuple(sorted(set(completed_sources))) or any(
        source_index > len(intent.sources) for source_index in completed_sources
    ):
        raise ValueError("completed sources")
    for source_index in completed_sources:
        source_units = tuple(unit for unit in units if unit.source_index == source_index)
        if not source_units or any(unit.status != "confirmed" for unit in source_units):
            raise ValueError("completed source has unconfirmed units")
    resolved_peer_id = raw.get("resolved_peer_id")
    if resolved_peer_id is not None:
        resolved_peer_id = _nonzero_int(resolved_peer_id)
    completion_ready = _boolean(raw.get("completion_ready"))
    operation_id = _operation_id(raw.get("operation_id"))
    created_at = _nonempty_string(raw.get("created_at"))
    updated_at = _nonempty_string(raw.get("updated_at"))
    _timestamp(created_at, "created_at")
    _timestamp(updated_at, "updated_at")
    if any(unit.status != "planned" for unit in units) and resolved_peer_id is None:
        raise ValueError("sending unit without resolved peer")
    if completion_ready and (
        not units
        or resolved_peer_id is None
        or any(unit.status != "confirmed" for unit in units)
        or completed_sources != tuple(range(1, len(intent.sources) + 1))
    ):
        raise ValueError("invalid completion-ready state")
    return PendingUpload(
        schema_version=PENDING_SCHEMA_VERSION,
        operation_id=operation_id,
        owner=owner,
        intent=intent,
        units=units,
        resolved_peer_id=resolved_peer_id,
        completed_sources=completed_sources,
        completion_ready=completion_ready,
        created_at=created_at,
        updated_at=updated_at,
    )


def _intent_to_json(intent: UploadIntent) -> dict[str, Any]:
    return {
        "profile_name": intent.profile_name,
        "chat_id": intent.chat_id,
        "reply_message_id": intent.reply_message_id,
        "supports_streaming": intent.supports_streaming,
        "oversize_policy": intent.oversize_policy,
        "media_algorithm_version": intent.media_algorithm_version,
        "sources": [
            {"file": _file_to_json(source.file), "caption": source.caption}
            for source in intent.sources
        ],
    }


def _intent_from_json(raw: dict[str, Any]) -> UploadIntent:
    _require_exact_keys(raw, _INTENT_KEYS, "intent")
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list):
        raise TypeError("sources")
    sources = tuple(_source_from_json(source) for source in sources_raw)
    reply_message_id = raw.get("reply_message_id")
    if reply_message_id is not None:
        reply_message_id = _integer(reply_message_id)
    return UploadIntent(
        profile_name=_nonempty_string(raw.get("profile_name")),
        chat_id=_nonempty_string(raw.get("chat_id")),
        reply_message_id=reply_message_id,
        supports_streaming=_boolean(raw.get("supports_streaming")),
        oversize_policy=_nonempty_string(raw.get("oversize_policy")),
        sources=sources,
        media_algorithm_version=_nonempty_string(raw.get("media_algorithm_version")),
    )


def _unit_from_json(raw: Any) -> PendingUnit:
    if not isinstance(raw, dict):
        raise TypeError("unit")
    _require_exact_keys(raw, _UNIT_KEYS, "unit")
    files_raw = raw.get("files")
    random_ids_raw = raw.get("random_ids")
    message_ids_raw = raw.get("message_ids")
    if not isinstance(files_raw, list):
        raise TypeError("files")
    if not isinstance(random_ids_raw, list) or not isinstance(message_ids_raw, list):
        raise TypeError("ids")
    files = tuple(_file_from_json(file) for file in files_raw)
    random_ids = tuple(_random_id(value) for value in random_ids_raw)
    if len(random_ids) != len(files) or len(set(random_ids)) != len(random_ids):
        raise ValueError("random IDs")
    status = raw.get("status")
    if status not in UNIT_STATUSES:
        raise ValueError("status")
    message_ids = tuple(_positive_int(value) for value in message_ids_raw)
    if status == "confirmed":
        _validate_message_ids(message_ids, expected_count=len(random_ids))
    elif message_ids:
        raise ValueError("unconfirmed message IDs")
    spec = UploadUnitSpec(
        key=_nonempty_string(raw.get("key")),
        source_index=_positive_int(raw.get("source_index")),
        preparation=_preparation(raw.get("preparation")),
        files=files,
    )
    return PendingUnit(
        key=spec.key,
        source_index=spec.source_index,
        preparation=spec.preparation,
        files=spec.files,
        random_ids=random_ids,
        status=status,
        message_ids=message_ids,
    )


def _file_to_json(file: FileIdentity) -> dict[str, Any]:
    return {"path": file.path, "size": file.size, "sha256": file.sha256}


def _file_from_json(raw: Any) -> FileIdentity:
    if not isinstance(raw, dict):
        raise TypeError("file")
    _require_exact_keys(raw, _FILE_KEYS, "file")
    return FileIdentity(
        path=_nonempty_string(raw.get("path")),
        size=_positive_int(raw.get("size")),
        sha256=_sha256_value(raw.get("sha256")),
    )


def _source_from_json(raw: Any) -> SourceIntent:
    if not isinstance(raw, dict):
        raise TypeError("source")
    _require_exact_keys(raw, _SOURCE_KEYS, "source")
    return SourceIntent(
        file=_file_from_json(_object(raw, "file")),
        caption=_string(raw.get("caption")),
    )


def _find_unit(pending: PendingUpload, key: str) -> tuple[int, PendingUnit]:
    for index, unit in enumerate(pending.units):
        if unit.key == key:
            return index, unit
    raise AppError(f"pending upload unit {key!r} does not exist")


def _is_discardable_planned(pending: PendingUpload) -> bool:
    """Return whether replacing this checkpoint cannot discard a final send attempt."""

    return (
        not pending.completion_ready
        and not pending.completed_sources
        and all(unit.status == "planned" for unit in pending.units)
    )


def _validate_message_ids(values: tuple[int, ...], *, expected_count: int) -> tuple[int, ...]:
    validated = tuple(_positive_int(value) for value in values)
    if len(validated) != expected_count or len(set(validated)) != len(validated):
        raise AppError("Telegram message ID count or identity does not match the upload unit")
    return validated


def _new_random_id(used_ids: set[int]) -> int:
    while True:
        value = int.from_bytes(secrets.token_bytes(8), "little", signed=True)
        if value != 0 and value not in used_ids:
            used_ids.add(value)
            return value


def _random_id(value: Any) -> int:
    parsed = _nonzero_int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        raise ValueError("random ID range")
    return parsed


def _fingerprint(domain: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_status = path.lstat()
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISDIR(path_status.st_mode):
            raise OSError("path is not a real directory")
        if os.name == "posix":
            path.chmod(0o700)
    except OSError as exc:
        raise AppError(f"failed to prepare private MTProto state directory {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_exact_keys(raw: dict[str, Any], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise ValueError(f"unexpected {label} fields")


def _operation_id(value: Any) -> str:
    parsed = _string(value)
    if len(parsed) != 32 or any(character not in "0123456789abcdef" for character in parsed):
        raise ValueError("operation_id")
    return parsed


def _timestamp(value: Any, label: str) -> datetime:
    parsed = _nonempty_string(value)
    try:
        timestamp = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise ValueError(label) from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError(label)
    return timestamp


def _sha256_value(value: Any) -> str:
    if not _is_sha256(value):
        raise ValueError("sha256")
    return value


def _object(raw: Any, key: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get(key), dict):
        raise TypeError(key)
    return raw[key]


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("string")
    return value


def _nonempty_string(value: Any) -> str:
    parsed = _string(value)
    if not parsed:
        raise ValueError("empty string")
    return parsed


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("boolean")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("integer")
    return value


def _positive_int(value: Any) -> int:
    parsed = _integer(value)
    if parsed <= 0:
        raise ValueError("positive integer")
    return parsed


def _nonzero_int(value: Any) -> int:
    parsed = _integer(value)
    if parsed == 0:
        raise ValueError("non-zero integer")
    return parsed


def _preparation(value: Any) -> PreparationKind:
    if value not in PREPARATION_KINDS:
        raise ValueError("preparation")
    return value
