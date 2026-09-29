from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest

from tg_comment_uploader.cli import (
    COMPRESS_MEDIA_TARGET_BYTES,
    SAFE_UPLOAD_LIMIT_BYTES,
    SPLIT_MEDIA_TARGET_BYTES,
    AppConfig,
    BotConfig,
    ProfileConfig,
    build_parser,
    partition_media_groups,
    prepared_media_target_bytes,
    route_compression_progress,
    route_split_progress,
    run_upload_locked,
    upload_media_group_with_retries,
)
from tg_comment_uploader.errors import NonRetryableUploadError, RetryableUploadError
from tg_comment_uploader.media_compress import CompressionProgress
from tg_comment_uploader.media_split import SplitProgress
from tg_comment_uploader.media_workflow import (
    PreparedMedia,
    prepare_media as prepare_media_workflow,
)
from tg_comment_uploader.telegram_sender import TelegramSender, UploadItem
from tg_comment_uploader.upload_state import (
    FileIdentity,
    PendingUnit,
    SourceIntent,
)


def fake_profile() -> ProfileConfig:
    return ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=42,
        caption="{stem}",
        supports_streaming=True,
    )


def fake_config() -> AppConfig:
    return AppConfig(
        bot=BotConfig(token="123456:TEST", api_id=1, api_hash="hash"),
        profiles={"default": fake_profile()},
    )


def file_identity(path: Path, marker: str = "a") -> FileIdentity:
    return FileIdentity(str(path.absolute()), max(path.stat().st_size, 1), marker * 64)


def test_parser_has_one_oversize_policy_and_short_alias() -> None:
    parser = build_parser()

    assert parser.parse_args(["upload", "/videos/a.mp4"]).oversize_policy == "error"
    assert parser.parse_args(["upload", "-o", "split", "/videos/a.mp4"]).oversize_policy == "split"
    assert (
        parser.parse_args(
            ["upload", "--oversize-policy", "compress", "/videos/a.mp4"]
        ).oversize_policy
        == "compress"
    )
    assert "--auto-split" not in parser.format_help()
    assert "--auto-compress" not in parser.format_help()


def test_policy_specific_media_targets_follow_the_mtproto_limit() -> None:
    assert SAFE_UPLOAD_LIMIT_BYTES == 2_097_152_000
    assert SPLIT_MEDIA_TARGET_BYTES == 2_055_208_960
    assert COMPRESS_MEDIA_TARGET_BYTES == 1_992_294_400
    assert prepared_media_target_bytes("error") == SPLIT_MEDIA_TARGET_BYTES
    assert prepared_media_target_bytes("split") == SPLIT_MEDIA_TARGET_BYTES
    assert prepared_media_target_bytes("compress") == COMPRESS_MEDIA_TARGET_BYTES


@pytest.mark.parametrize(
    ("count", "expected_sizes"),
    [(2, [2]), (10, [10]), (11, [6, 5]), (20, [10, 10]), (21, [7, 7, 7])],
)
def test_media_group_partition_is_minimal_balanced_and_ordered(
    count: int,
    expected_sizes: list[int],
) -> None:
    paths = [Path(f"/videos/part-{index:04d}.mp4") for index in range(count)]

    groups = partition_media_groups(paths)

    assert [len(group) for group in groups] == expected_sizes
    assert [path for group in groups for path in group] == paths
    assert all(2 <= len(group) <= 10 for group in groups)


class RecordingRenderer:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def update(self, label: str, fraction: float) -> None:
        self.events.append(("update", label, fraction))

    def log(self, message: str) -> None:
        self.events.append(("log", message))

    def finish(self) -> None:
        self.events.append(("finish",))


def test_split_progress_router_keeps_structured_stage_output() -> None:
    renderer: Any = RecordingRenderer()
    route_split_progress(renderer, "[2/4]", SplitProgress(stage="probing"))
    route_split_progress(
        renderer,
        "[2/4]",
        SplitProgress(stage="planning", attempt=1, max_attempts=3, part_count=4),
    )
    route_split_progress(
        renderer,
        "[2/4]",
        SplitProgress(
            stage="splitting",
            attempt=1,
            max_attempts=3,
            part_count=4,
            fraction=0.375,
        ),
    )
    route_split_progress(
        renderer,
        "[2/4]",
        SplitProgress(stage="validating", attempt=1, max_attempts=3, part_count=4),
    )

    assert renderer.events == [
        ("log", "[2/4] probing media for lossless split"),
        ("log", "[2/4] planning lossless split attempt 1/3 (4 parts)"),
        ("update", "[2/4] split attempt 1/3 (4 parts)", 0.375),
        ("log", "[2/4] validating 4 split parts from attempt 1/3"),
    ]


def test_compression_progress_router_keeps_two_pass_output() -> None:
    renderer: Any = RecordingRenderer()
    route_compression_progress(renderer, "[1/2]", CompressionProgress(stage="probing"))
    route_compression_progress(renderer, "[1/2]", CompressionProgress(stage="planning"))
    route_compression_progress(
        renderer,
        "[1/2]",
        CompressionProgress(
            stage="compressing",
            attempt=2,
            max_attempts=3,
            pass_number=1,
            fraction=0.625,
        ),
    )
    route_compression_progress(
        renderer,
        "[1/2]",
        CompressionProgress(stage="validating", attempt=2, max_attempts=3),
    )

    assert renderer.events == [
        ("log", "[1/2] probing media for compression"),
        ("log", "[1/2] planning two-pass compression"),
        ("update", "[1/2] compress attempt 2/3 pass 1/2", 0.625),
        ("log", "[1/2] validating compressed output from attempt 2/3"),
    ]


class FakeState:
    def __init__(self) -> None:
        self.operation_id = "operation"
        self.units: dict[str, PendingUnit] = {}
        self.events: list[tuple[Any, ...]] = []
        self.next_random = 100

    def preflight_new_upload(self) -> None:
        return None

    def open_or_create(self, intent: Any, *, on_replaced: Any = None) -> Any:
        del on_replaced
        self.events.append(("open", intent))
        return SimpleNamespace(
            operation_id=self.operation_id,
            units=(),
            confirmed_unit_count=0,
            completed_sources=(),
            completion_ready=False,
            fully_confirmed=False,
        )

    def register_unit(self, spec: Any) -> PendingUnit:
        random_ids = tuple(range(self.next_random, self.next_random + len(spec.files)))
        self.next_random += len(spec.files)
        unit = PendingUnit(
            key=spec.key,
            source_index=spec.source_index,
            preparation=spec.preparation,
            files=spec.files,
            random_ids=random_ids,
            status="planned",
            message_ids=(),
        )
        self.units[spec.key] = unit
        self.events.append(("register", spec.key, len(spec.files)))
        return unit

    def bind_peer(self, peer_id: int) -> None:
        self.events.append(("peer", peer_id))

    def mark_sending(self, key: str) -> None:
        self.events.append(("sending", key))

    def mark_confirmed(self, key: str, message_ids: tuple[int, ...]) -> None:
        self.events.append(("confirmed", key, message_ids))

    def mark_source_completed(self, source_index: int) -> None:
        self.events.append(("source-completed", source_index))

    def complete(self, *, expected_unit_count: int) -> None:
        self.events.append(("complete", expected_unit_count))

    def inspect(self) -> Any:
        return SimpleNamespace(operation_id=self.operation_id, units=tuple(self.units.values()))


class FakeAlbumSender:
    def __init__(self) -> None:
        self.groups: list[tuple[tuple[UploadItem, ...], tuple[int, ...]]] = []
        self.closed = False

    def send_media_group(
        self,
        items: Any,
        *,
        random_ids: Any,
        progress_callback: Any = None,
        before_final_request: Any = None,
    ) -> tuple[int, ...]:
        frozen_items = tuple(items)
        frozen_ids = tuple(random_ids)
        self.groups.append((frozen_items, frozen_ids))
        for index, item in enumerate(frozen_items):
            if progress_callback is not None:
                progress_callback(index, item.expected_size or 1, item.expected_size or 1)
        if before_final_request is not None:
            before_final_request()
        base = 1000 + sum(len(group[0]) for group in self.groups[:-1])
        return tuple(range(base, base + len(frozen_items)))

    def send_video(self, *args: Any, **kwargs: Any) -> int:
        raise AssertionError("single video was not expected")

    def close(self) -> None:
        self.closed = True


def install_orchestration_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: FakeState,
    sender: Any,
) -> None:
    paths = SimpleNamespace(session_path=tmp_path / "session.session")
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr(
        "tg_comment_uploader.cli.create_pending_store",
        lambda config_path, bot: (paths, state),
    )

    def create_sender(*args: Any, **kwargs: Any) -> Any:
        kwargs["peer_resolved"](-1001234567890)
        return sender

    monkeypatch.setattr("tg_comment_uploader.cli.create_sender", create_sender)


def test_unverified_snapshot_stops_before_pending_registration_or_sender_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large.mp4"
    source.write_bytes(b"oversized")
    source_identity = FileIdentity(str(source), source.stat().st_size, "0" * 64)
    state = FakeState()
    sender = FakeAlbumSender()
    install_orchestration_fakes(tmp_path, monkeypatch, state, sender)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.fingerprint_sources",
        lambda files, captions, allow_oversized: (SourceIntent(source_identity, "large"),),
    )

    def forbidden_compress(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("FFmpeg must not receive an unverified snapshot")

    monkeypatch.setattr(
        "tg_comment_uploader.media_workflow.compress_video",
        forbidden_compress,
    )

    @contextmanager
    def force_oversized_prepare(
        path: Path,
        policy: str,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        kwargs["hard_limit_bytes"] = 5
        kwargs["target_bytes"] = 4
        with prepare_media_workflow(path, cast(Any, policy), **kwargs) as prepared:
            yield prepared

    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", force_oversized_prepare)
    args = build_parser().parse_args(["upload", "-o", "compress", str(source)])

    with pytest.raises(NonRetryableUploadError, match="snapshot identity does not match"):
        run_upload_locked(args)

    assert not any(event[0] == "register" for event in state.events)
    assert sender.groups == []
    assert sender.closed is False
    assert not (tmp_path / ".tg-comment-uploader-work").exists()


def test_split_11_parts_stays_6_plus_5_with_each_group_captioned_and_checkpointed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source video.mp4"
    with source.open("wb") as output:
        output.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    parts = tuple(tmp_path / f"source video part-{index:04d}.mp4" for index in range(1, 12))
    for index, part in enumerate(parts):
        part.write_bytes(f"part-{index}".encode())
    state = FakeState()
    sender = FakeAlbumSender()
    install_orchestration_fakes(tmp_path, monkeypatch, state, sender)
    source_identity = FileIdentity(str(source), SAFE_UPLOAD_LIMIT_BYTES + 1, "a" * 64)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.fingerprint_sources",
        lambda files, captions, allow_oversized: (SourceIntent(source_identity, "source video"),),
    )
    lifecycle: list[str] = []

    @contextmanager
    def fake_prepare(path: Path, policy: str, **kwargs: Any) -> Iterator[PreparedMedia]:
        assert kwargs["hard_limit_bytes"] == SAFE_UPLOAD_LIMIT_BYTES
        assert kwargs["target_bytes"] == SPLIT_MEDIA_TARGET_BYTES
        assert kwargs["expected_source_size"] == source_identity.size
        assert kwargs["expected_source_sha256"] == source_identity.sha256
        lifecycle.append("prepared")
        yield PreparedMedia(source=source, paths=parts, policy="split")
        lifecycle.append("cleaned")

    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare)
    args = build_parser().parse_args(["upload", "-o", "split", str(source)])

    assert run_upload_locked(args) == 0

    assert [len(items) for items, _ in sender.groups] == [6, 5]
    assert [item.path for group, _ in sender.groups for item in group] == list(parts)
    for items, _ in sender.groups:
        assert items[0].caption == "source video"
        assert all(item.caption == "" for item in items[1:])
        assert all(item.expected_size is not None and item.expected_sha256 for item in items)
    assert [event[:3] for event in state.events if event[0] == "register"] == [
        ("register", "source:1/group:1", 6),
        ("register", "source:1/group:2", 5),
    ]
    assert [event[0] for event in state.events].count("sending") == 2
    assert [event[0] for event in state.events].count("confirmed") == 2
    assert state.events[-1] == ("complete", 2)
    assert lifecycle == ["prepared", "cleaned"]
    assert sender.closed is True


def test_compress_policy_keeps_target_and_routes_one_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "large.mp4"
    with source.open("wb") as output:
        output.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    compressed = tmp_path / "compressed.mp4"
    compressed.write_bytes(b"compressed")
    state = FakeState()

    class SingleSender(FakeAlbumSender):
        def __init__(self) -> None:
            super().__init__()
            self.item: UploadItem | None = None

        def send_video(self, item: UploadItem, **kwargs: Any) -> int:
            self.item = item
            kwargs["before_final_request"]()
            return 77

    sender = SingleSender()
    install_orchestration_fakes(tmp_path, monkeypatch, state, sender)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.fingerprint_sources",
        lambda files, captions, allow_oversized: (
            SourceIntent(
                FileIdentity(str(source), SAFE_UPLOAD_LIMIT_BYTES + 1, "a" * 64),
                "large",
            ),
        ),
    )
    captured: dict[str, Any] = {}

    @contextmanager
    def fake_prepare(path: Path, policy: str, **kwargs: Any) -> Iterator[PreparedMedia]:
        captured.update(kwargs)
        assert policy == "compress"
        yield PreparedMedia(source=source, paths=(compressed,), policy="compress")

    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare)
    args = build_parser().parse_args(["upload", "-o", "compress", str(source)])

    assert run_upload_locked(args) == 0
    assert captured["target_bytes"] == COMPRESS_MEDIA_TARGET_BYTES
    assert captured["expected_source_size"] == SAFE_UPLOAD_LIMIT_BYTES + 1
    assert captured["expected_source_sha256"] == "a" * 64
    assert sender.item is not None
    assert sender.item.path == compressed
    assert sender.item.caption == "large"
    assert ("register", "source:1/single", 1) in state.events


def test_media_group_retry_reuses_paths_and_random_id_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = (tmp_path / "one.mp4", tmp_path / "two.mp4")
    for path in paths:
        path.write_bytes(b"video")
    items = tuple(
        UploadItem(
            path,
            "",
            expected_size=path.stat().st_size,
            expected_sha256="0" * 64,
        )
        for path in paths
    )
    unit = PendingUnit(
        key="source:1/group:1",
        source_index=1,
        preparation="split",
        files=tuple(file_identity(path) for path in paths),
        random_ids=(91, 92),
        status="planned",
        message_ids=(),
    )
    calls: list[tuple[tuple[Path, ...], tuple[int, ...]]] = []

    class FlakySender:
        def send_media_group(self, sent_items: Any, *, random_ids: Any, **kwargs: Any) -> Any:
            calls.append((tuple(item.path for item in sent_items), tuple(random_ids)))
            if len(calls) == 1:
                raise RetryableUploadError("temporary")
            return (1, 2)

    monkeypatch.setattr("tg_comment_uploader.cli.time.sleep", lambda seconds: None)
    result = upload_media_group_with_retries(
        cast(TelegramSender, FlakySender()),
        items,
        unit=unit,
        label="group",
        retries=1,
    )

    assert result == (1, 2)
    assert calls == [(paths, (91, 92)), (paths, (91, 92))]
