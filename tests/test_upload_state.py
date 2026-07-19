from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tg_comment_uploader.errors import AppError
from tg_comment_uploader.upload_state import (
    DEFAULT_MEDIA_ALGORITHM_VERSION,
    PENDING_SCHEMA_VERSION,
    FileIdentity,
    PendingOwner,
    PendingReplacementKind,
    PendingUpload,
    PendingUploadStore,
    SourceIntent,
    UploadIntent,
    UploadUnitSpec,
    get_mtproto_paths,
)


def identity(path: Path, marker: str = "a") -> FileIdentity:
    return FileIdentity(path=str(path.absolute()), size=10, sha256=marker * 64)


def intent(
    tmp_path: Path,
    *,
    caption: str = "caption",
    reply_message_id: int | None = 123,
    media_algorithm_version: str = DEFAULT_MEDIA_ALGORITHM_VERSION,
) -> UploadIntent:
    return UploadIntent(
        profile_name="default",
        chat_id="-1001234567890",
        reply_message_id=reply_message_id,
        supports_streaming=True,
        oversize_policy="split",
        sources=(SourceIntent(identity(tmp_path / "source.mp4"), caption),),
        media_algorithm_version=media_algorithm_version,
    )


def spec(
    tmp_path: Path,
    *,
    marker: str = "b",
    count: int = 1,
    key: str | None = None,
) -> UploadUnitSpec:
    return UploadUnitSpec(
        key=key or ("source:1/single" if count == 1 else "source:1/group:1"),
        source_index=1,
        preparation="original" if count == 1 else "split",
        files=tuple(identity(tmp_path / f"part-{index}.mp4", marker) for index in range(count)),
    )


def store(tmp_path: Path, *, owner: PendingOwner | None = None) -> PendingUploadStore:
    selected_owner = owner or PendingOwner(config_fingerprint="c" * 64, bot_id=123456)
    return PendingUploadStore(tmp_path / "state" / "pending.json", owner=selected_owner)


def test_paths_are_project_local_private_names_and_isolate_credentials(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("", encoding="utf-8")
    config = tmp_path / "private config.json"
    config.write_text("{}", encoding="utf-8")

    first = get_mtproto_paths(
        config,
        "123456:SECRET_ONE",
        api_id=10,
        api_hash="HASH_ONE",
        project_root=tmp_path,
    )
    second = get_mtproto_paths(
        config,
        "123456:SECRET_TWO",
        api_id=10,
        api_hash="HASH_ONE",
        project_root=tmp_path,
    )
    other_config = tmp_path / "other config.json"
    other_config.write_text("{}", encoding="utf-8")
    third = get_mtproto_paths(
        other_config,
        "123456:SECRET_ONE",
        api_id=10,
        api_hash="HASH_ONE",
        project_root=tmp_path,
    )

    assert first.pending_path == (
        tmp_path / ".local/tg-comment-uploader/mtproto/pending-upload-v1.json"
    )
    assert first.session_path.parent == (tmp_path / ".local/tg-comment-uploader/mtproto/sessions")
    assert first.session_path.suffix == ".session"
    assert first.session_path != second.session_path
    assert first.session_path != third.session_path
    assert first.owner == second.owner
    rendered = str(first.session_path)
    assert "SECRET" not in rendered
    assert "HASH_ONE" not in rendered
    assert "private config" not in rendered


def test_create_register_is_durable_idempotent_and_private(tmp_path: Path) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path, count=2))
    reloaded = store(tmp_path).inspect()

    assert pending.schema_version == PENDING_SCHEMA_VERSION
    assert len(pending.operation_id) == 32
    assert len(unit.random_ids) == 2
    assert len(set(unit.random_ids)) == 2
    assert all(-(2**63) <= value <= 2**63 - 1 and value != 0 for value in unit.random_ids)
    assert state.register_unit(spec(tmp_path, count=2)).random_ids == unit.random_ids
    assert reloaded is not None
    assert reloaded.units[0] == unit
    if os.name == "posix":
        assert state.path.stat().st_mode & 0o777 == 0o600
        assert state.path.parent.stat().st_mode & 0o777 == 0o700


def test_state_machine_peer_binding_confirmation_and_completion(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    planned = state.register_unit(spec(tmp_path, count=2))

    bound = state.bind_peer(-1001234567890)
    sending = state.mark_sending(planned.key)
    confirmed = state.mark_confirmed(planned.key, (41, 42))

    assert bound.resolved_peer_id == -1001234567890
    assert sending.status == "sending"
    assert confirmed.status == "confirmed"
    assert confirmed.message_ids == (41, 42)
    assert state.mark_sending(planned.key) == confirmed
    assert state.mark_confirmed(planned.key, (41, 42)) == confirmed
    completed = state.mark_source_completed(1)
    assert completed.completed_sources == (1,)
    state.complete(expected_unit_count=1)
    assert state.inspect() is None


def test_durable_completion_boundary_requires_the_matching_command_to_finish_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = store(tmp_path)
    original = intent(tmp_path)
    pending = state.open_or_create(original)
    unit = state.register_unit(spec(tmp_path))
    state.bind_peer(-1001234567890)
    state.mark_sending(unit.key)
    state.mark_confirmed(unit.key, (41,))
    state.mark_source_completed(1)

    original_unlink = Path.unlink

    def fail_checkpoint_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == state.path:
            raise OSError("directory became read-only")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_checkpoint_unlink)
    with pytest.raises(AppError, match="failed to remove"):
        state.complete(expected_unit_count=1)
    restored = state.inspect()
    assert restored is not None
    assert restored.operation_id == pending.operation_id
    assert restored.completion_ready is True

    monkeypatch.undo()
    replacements: list[tuple[PendingUpload, PendingReplacementKind]] = []
    replacement = state.open_or_create(
        intent(tmp_path, caption="a new command"),
        on_replaced=lambda previous, kind: replacements.append((previous, kind)),
    )

    assert replacement.operation_id != pending.operation_id
    assert replacement.intent.sources[0].caption == "a new command"
    assert replacements == [(restored, "fully-confirmed")]
    assert state.inspect() == replacement


def test_matching_planned_intent_resumes_without_replacing_checkpoint(tmp_path: Path) -> None:
    state = store(tmp_path)
    original = intent(tmp_path)
    pending = state.open_or_create(original)
    unit = state.register_unit(spec(tmp_path))

    resumed = state.open_or_create(original)

    assert resumed.operation_id == pending.operation_id
    assert resumed.units == (unit,)


def test_v1_planned_checkpoint_is_replaced_by_current_media_algorithm(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    legacy = state.open_or_create(intent(tmp_path, media_algorithm_version="1"))
    state.register_unit(spec(tmp_path, count=2))
    legacy_planned = state.inspect()
    assert legacy_planned is not None
    replacements: list[tuple[PendingUpload, PendingReplacementKind]] = []
    upgraded = store(tmp_path)

    replacement = upgraded.open_or_create(
        intent(tmp_path),
        on_replaced=lambda previous, kind: replacements.append((previous, kind)),
    )

    assert DEFAULT_MEDIA_ALGORITHM_VERSION == "2"
    assert PENDING_SCHEMA_VERSION == 1
    assert replacement.operation_id != legacy.operation_id
    assert replacement.intent.media_algorithm_version == "2"
    assert replacement.units == ()
    assert replacements == [(legacy_planned, "planned")]
    assert upgraded.inspect() == replacement


@pytest.mark.parametrize("legacy_stage", ["sending", "partially-confirmed"])
def test_v1_nonplanned_checkpoint_blocks_current_media_algorithm(
    tmp_path: Path,
    legacy_stage: str,
) -> None:
    state = store(tmp_path)
    legacy = state.open_or_create(intent(tmp_path, media_algorithm_version="1"))
    first = state.register_unit(spec(tmp_path, count=2, key="source:1/group:1"))
    if legacy_stage == "partially-confirmed":
        state.register_unit(spec(tmp_path, marker="d", count=2, key="source:1/group:2"))
    state.bind_peer(-1001234567890)
    state.mark_sending(first.key)
    if legacy_stage == "partially-confirmed":
        state.mark_confirmed(first.key, (41, 42))
    before = state.path.read_bytes()
    upgraded = store(tmp_path)

    with pytest.raises(AppError, match="does not match this command"):
        upgraded.open_or_create(intent(tmp_path))

    restored = upgraded.inspect()
    assert restored is not None
    assert restored.operation_id == legacy.operation_id
    assert restored.intent.media_algorithm_version == "1"
    assert restored.stage == legacy_stage
    assert state.path.read_bytes() == before


def test_mismatched_intent_atomically_replaces_empty_planned_checkpoint(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))

    replacement = state.open_or_create(intent(tmp_path, caption="a new command"))

    assert replacement.operation_id != pending.operation_id
    assert replacement.intent.sources[0].caption == "a new command"
    assert replacement.units == ()
    assert replacement.resolved_peer_id is None
    assert replacement.completed_sources == ()
    assert replacement.completion_ready is False
    assert state.inspect() == replacement


def test_mismatched_intent_replaces_only_planned_units_even_after_peer_binding(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))
    state.bind_peer(-1001234567890)

    replacement = state.open_or_create(intent(tmp_path, caption="a new command"))

    assert replacement.operation_id != pending.operation_id
    assert replacement.units == ()
    assert replacement.resolved_peer_id is None
    assert state.inspect() == replacement


def test_planned_checkpoint_can_be_replaced_across_config_and_bot_owner(
    tmp_path: Path,
) -> None:
    original_store = store(tmp_path)
    original = original_store.open_or_create(intent(tmp_path))
    original_store.register_unit(spec(tmp_path))
    other_owner = PendingOwner(config_fingerprint="d" * 64, bot_id=999)
    replacement_store = store(tmp_path, owner=other_owner)
    replacements: list[tuple[PendingUpload, PendingReplacementKind]] = []

    replacement_store.preflight_new_upload()
    replacement = replacement_store.open_or_create(
        intent(tmp_path, caption="other owner command"),
        on_replaced=lambda previous, kind: replacements.append((previous, kind)),
    )

    assert replacement.operation_id != original.operation_id
    assert replacement.owner == other_owner
    assert replacements[0][0].operation_id == original.operation_id
    assert replacements[0][1] == "planned"
    assert replacement_store.inspect() == replacement


def test_foreign_nonplanned_checkpoint_is_rejected_before_and_after_hashing_boundary(
    tmp_path: Path,
) -> None:
    original_store = store(tmp_path)
    original_store.open_or_create(intent(tmp_path))
    unit = original_store.register_unit(spec(tmp_path))
    original_store.bind_peer(-1001234567890)
    original_store.mark_sending(unit.key)
    before = original_store.path.read_bytes()
    replacement_store = store(
        tmp_path,
        owner=PendingOwner(config_fingerprint="d" * 64, bot_id=999),
    )

    with pytest.raises(AppError, match="another config or bot"):
        replacement_store.preflight_new_upload()
    with pytest.raises(AppError, match="another config or bot"):
        replacement_store.open_or_create(intent(tmp_path, caption="other owner command"))
    assert original_store.path.read_bytes() == before


def test_failed_planned_replacement_preserves_the_old_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path))
    before = state.path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(AppError, match="failed to persist"):
        state.open_or_create(intent(tmp_path, caption="a new command"))

    assert state.path.read_bytes() == before
    restored = state.inspect()
    assert restored is not None
    assert restored.operation_id == pending.operation_id
    assert restored.units == (unit,)
    assert list(state.path.parent.glob(".*.tmp")) == []


def test_intent_spec_owner_and_peer_mismatches_are_refused(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path))
    state.bind_peer(-1001)
    state.mark_sending(unit.key)

    with pytest.raises(AppError, match="does not match this command"):
        state.open_or_create(intent(tmp_path, caption="changed"))
    with pytest.raises(AppError, match="prepared media.*changed"):
        state.register_unit(spec(tmp_path, marker="d"))
    with pytest.raises(AppError, match="different peer"):
        state.bind_peer(-1002)
    with pytest.raises(AppError, match="another config or bot"):
        store(
            tmp_path,
            owner=PendingOwner(config_fingerprint="e" * 64, bot_id=999),
        ).inspect()


def test_mismatched_intent_blocks_partial_confirmation_but_replaces_terminal_residual(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path))
    state.bind_peer(-1001234567890)
    state.mark_sending(unit.key)
    state.mark_confirmed(unit.key, (41,))

    with pytest.raises(AppError, match="does not match this command"):
        state.open_or_create(intent(tmp_path, caption="a new command"))
    confirmed = state.inspect()
    assert confirmed is not None
    assert confirmed.operation_id == pending.operation_id
    assert confirmed.units[0].status == "confirmed"

    completed = state.mark_source_completed(1)
    assert completed.completion_ready is True
    replacement = state.open_or_create(intent(tmp_path, caption="a new command"))
    assert replacement.operation_id != completed.operation_id
    assert replacement.intent.sources[0].caption == "a new command"
    assert state.inspect() == replacement


def test_random_ids_survive_sending_and_process_style_reopen(tmp_path: Path) -> None:
    first = store(tmp_path)
    first.open_or_create(intent(tmp_path))
    registered = first.register_unit(spec(tmp_path))
    first.bind_peer(-1001234567890)
    first.mark_sending(registered.key)

    reopened = store(tmp_path)
    restored = reopened.open_or_create(intent(tmp_path))
    same = reopened.register_unit(spec(tmp_path))

    assert restored.units[0].status == "sending"
    assert same.random_ids == registered.random_ids


def test_pending_stage_never_labels_confirmed_work_as_planned(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    first = state.register_unit(spec(tmp_path, key="source:1/group:1"))
    second = state.register_unit(spec(tmp_path, marker="d", key="source:1/group:2"))
    pending = state.inspect()
    assert pending is not None
    assert pending.stage == "planned"

    state.bind_peer(-1001234567890)
    state.mark_sending(first.key)
    pending = state.inspect()
    assert pending is not None
    assert pending.stage == "sending"

    state.mark_confirmed(first.key, (41,))
    pending = state.inspect()
    assert pending is not None
    assert pending.stage == "partially-confirmed"

    state.mark_sending(second.key)
    pending = state.inspect()
    assert pending is not None
    assert pending.stage == "sending"

    state.mark_confirmed(second.key, (42,))
    pending = state.inspect()
    assert pending is not None
    assert pending.stage == "partially-confirmed"

    completed = state.mark_source_completed(1)
    assert completed.stage == "confirmed"


def test_confirm_requires_sending_and_exact_unique_message_ids(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path, count=2))

    with pytest.raises(AppError, match="not marked sending"):
        state.mark_confirmed(unit.key, (1, 2))
    state.bind_peer(-1001234567890)
    state.mark_sending(unit.key)
    with pytest.raises(AppError, match="count or identity"):
        state.mark_confirmed(unit.key, (1,))
    with pytest.raises(AppError, match="count or identity"):
        state.mark_confirmed(unit.key, (1, 1))


def test_complete_refuses_missing_or_unconfirmed_units(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))

    with pytest.raises(AppError, match="unit count"):
        state.complete(expected_unit_count=2)
    with pytest.raises(AppError, match="unconfirmed"):
        state.complete(expected_unit_count=1)
    assert state.path.exists()


def test_empty_pending_operation_can_be_removed_before_any_send_unit(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))

    state.complete(expected_unit_count=0)

    assert state.inspect() is None


def test_directory_fsync_failure_after_unlink_does_not_report_checkpoint_as_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))

    def fail_directory_fsync(path: Path) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(
        "tg_comment_uploader.upload_state._fsync_directory",
        fail_directory_fsync,
    )
    state.complete(expected_unit_count=0)

    assert not state.path.exists()


def test_source_completion_requires_all_of_its_units_to_be_confirmed(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path))

    with pytest.raises(AppError, match="unconfirmed"):
        state.mark_source_completed(1)

    state.bind_peer(-1001234567890)
    state.mark_sending(unit.key)
    state.mark_confirmed(unit.key, (41,))
    state.mark_source_completed(1)

    with pytest.raises(AppError, match="completed (?:pending upload|upload source)"):
        state.register_unit(spec(tmp_path, marker="d", key="source:1/group:2"))


def test_discard_requires_exact_operation_id(tmp_path: Path) -> None:
    state = store(tmp_path)
    pending = state.open_or_create(intent(tmp_path))

    with pytest.raises(AppError, match="operation ID"):
        state.discard("wrong")
    assert state.discard(pending.operation_id) is False
    assert state.inspect() is None


def test_foreign_owner_status_is_read_only_and_discard_requires_force_and_exact_id(
    tmp_path: Path,
) -> None:
    original_store = store(tmp_path)
    pending = original_store.open_or_create(intent(tmp_path))
    unit = original_store.register_unit(spec(tmp_path))
    original_store.bind_peer(-1001234567890)
    original_store.mark_sending(unit.key)
    before = original_store.path.read_bytes()
    maintenance_store = store(
        tmp_path,
        owner=PendingOwner(config_fingerprint="f" * 64, bot_id=1),
    )

    inspected = maintenance_store.inspect_any_owner()
    assert inspected is not None
    assert inspected.operation_id == pending.operation_id
    assert maintenance_store.path.read_bytes() == before
    with pytest.raises(AppError, match="--force-foreign-owner"):
        maintenance_store.discard(pending.operation_id)
    with pytest.raises(AppError, match="operation ID"):
        maintenance_store.discard("wrong", force_foreign_owner=True)
    assert maintenance_store.path.read_bytes() == before

    assert maintenance_store.discard(pending.operation_id, force_foreign_owner=True) is True
    assert not maintenance_store.path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[" * 2_000 + "0" + "]" * 2_000,
        json.dumps({"schema_version": 999}),
        json.dumps({"schema_version": PENDING_SCHEMA_VERSION}),
    ],
)
def test_corrupt_or_unknown_state_never_silently_restarts(tmp_path: Path, raw: str) -> None:
    state = store(tmp_path)
    state.path.parent.mkdir(parents=True)
    state.path.write_text(raw, encoding="utf-8")

    with pytest.raises(AppError, match="corrupt|invalid|unsupported"):
        state.open_or_create(intent(tmp_path))
    assert state.path.read_text(encoding="utf-8") == raw


def test_failed_atomic_replace_preserves_previous_checkpoint_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    unit = state.register_unit(spec(tmp_path))
    state.bind_peer(-1001234567890)
    before = state.path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(AppError, match="failed to persist"):
        state.mark_sending(unit.key)

    assert state.path.read_bytes() == before
    assert list(state.path.parent.glob(".*.tmp")) == []
    restored = store(tmp_path).inspect()
    assert restored is not None
    assert restored.units[0].status == "planned"


def test_state_json_contains_no_credentials_or_plain_config_path(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("", encoding="utf-8")
    config = tmp_path / "do-not-persist-this-config-name.json"
    config.write_text("{}", encoding="utf-8")
    paths = get_mtproto_paths(
        config,
        "123456:DO_NOT_PERSIST_THIS_TOKEN",
        api_id=10,
        api_hash="DO_NOT_PERSIST_THIS_API_HASH",
        project_root=tmp_path,
    )
    state = PendingUploadStore(paths.pending_path, owner=paths.owner)
    state.open_or_create(intent(tmp_path))
    content = state.path.read_text(encoding="utf-8")

    assert "DO_NOT_PERSIST_THIS_TOKEN" not in content
    assert "DO_NOT_PERSIST_THIS_API_HASH" not in content
    assert str(config) not in content
    assert config.name not in content
    assert '"config_fingerprint"' in content


def test_sending_requires_a_bound_peer_and_message_ids_are_globally_unique(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    first = state.register_unit(spec(tmp_path, key="source:1/group:1"))
    second = state.register_unit(spec(tmp_path, marker="d", key="source:1/group:2"))

    with pytest.raises(AppError, match="before binding"):
        state.mark_sending(first.key)

    state.bind_peer(-1001234567890)
    state.mark_sending(first.key)
    state.mark_confirmed(first.key, (41,))
    state.mark_sending(second.key)
    with pytest.raises(AppError, match="conflict"):
        state.mark_confirmed(second.key, (41,))


def test_existing_state_permissions_are_tightened_when_read(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode bits are not available")

    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.path.chmod(0o644)

    assert state.inspect() is not None
    assert state.path.stat().st_mode & 0o777 == 0o600


def test_symlink_state_is_never_treated_as_absent_or_followed(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("creating symlinks is not reliably available on Windows")

    state = store(tmp_path)
    state.path.parent.mkdir(parents=True)
    state.path.symlink_to(tmp_path / "missing-target.json")

    with pytest.raises(AppError, match="not a regular file"):
        state.open_or_create(intent(tmp_path))
    assert state.path.is_symlink()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("operation_id", "not-a-uuid"),
        ("created_at", "not-a-timestamp"),
    ],
)
def test_valid_json_with_invalid_scalar_fields_is_rejected(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw[field] = value
    state.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AppError, match="invalid|unsupported"):
        state.inspect()


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw["unexpected"] = "ignored by a permissive decoder"
    state.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AppError, match="invalid|unsupported"):
        state.inspect()


def test_duplicate_json_fields_are_rejected_before_pending_schema_parsing(
    tmp_path: Path,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    payload = state.path.read_text(encoding="utf-8")
    field = '"reply_message_id": 123,'
    assert payload.count(field) == 1
    payload = payload.replace(field, f'{field}\n    "reply_message_id": null,', 1)
    state.path.write_text(payload, encoding="utf-8")

    with pytest.raises(AppError, match="unreadable or corrupt"):
        state.inspect()


def test_upload_intent_enforces_reply_message_signed_int32_bound() -> None:
    maximum = 2_147_483_647

    assert intent(Path("/tmp"), reply_message_id=maximum).reply_message_id == maximum
    with pytest.raises(ValueError, match="signed 32-bit"):
        intent(Path("/tmp"), reply_message_id=maximum + 1)


def test_unit_cannot_refer_to_a_source_outside_the_intent(tmp_path: Path) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw["units"][0]["source_index"] = 2
    state.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AppError, match="invalid|unsupported"):
        state.inspect()


@pytest.mark.parametrize("random_id", [-(2**63), 2**63 - 1])
def test_signed_64_bit_random_id_boundaries_are_accepted(
    tmp_path: Path,
    random_id: int,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw["units"][0]["random_ids"] = [random_id]
    state.path.write_text(json.dumps(raw), encoding="utf-8")

    restored = state.inspect()
    assert restored is not None
    assert restored.units[0].random_ids == (random_id,)


@pytest.mark.parametrize("random_id", [-(2**63) - 1, 2**63, 0, True])
def test_invalid_signed_64_bit_random_ids_are_rejected(
    tmp_path: Path,
    random_id: int,
) -> None:
    state = store(tmp_path)
    state.open_or_create(intent(tmp_path))
    state.register_unit(spec(tmp_path))
    raw = json.loads(state.path.read_text(encoding="utf-8"))
    raw["units"][0]["random_ids"] = [random_id]
    state.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(AppError, match="invalid|unsupported"):
        state.inspect()
