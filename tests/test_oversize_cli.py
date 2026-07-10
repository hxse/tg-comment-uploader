from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from tg_comment_uploader.cli import (
    COMPRESS_MEDIA_TARGET_BYTES,
    SAFE_UPLOAD_LIMIT_BYTES,
    SPLIT_MEDIA_TARGET_BYTES,
    AppConfig,
    BotConfig,
    NonRetryableUploadError,
    ProfileConfig,
    RetryableUploadError,
    ServerConfig,
    build_media_group_payload,
    build_parser,
    partition_media_groups,
    prepared_media_target_bytes,
    route_compression_progress,
    route_split_progress,
    run_upload_locked,
    send_media_group,
    upload_media_groups_with_retries,
    validate_upload_paths,
)
from tg_comment_uploader.media_compress import CompressionProgress
from tg_comment_uploader.media_split import SplitProgress
from tg_comment_uploader.media_workflow import MediaPreparationError, PreparedMedia


def fake_profile(reply_message_id: int | None = 12345) -> ProfileConfig:
    return ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=reply_message_id,
        caption="{stem}",
        supports_streaming=True,
    )


def fake_config(profile: ProfileConfig | None = None) -> AppConfig:
    selected = profile or fake_profile()
    return AppConfig(
        bot=BotConfig(token="token", api_id=1, api_hash="hash"),
        server=ServerConfig(
            host="127.0.0.1",
            port=48973,
            binary="telegram-bot-api",
            work_dir=Path("."),
        ),
        profiles={"default": selected},
    )


def test_parser_has_one_oversize_policy_with_error_default() -> None:
    parser = build_parser()

    default_args = parser.parse_args(["upload", "/videos/a.mp4"])
    split_args = parser.parse_args(["upload", "--oversize-policy", "split", "/videos/a.mp4"])
    compress_args = parser.parse_args(["upload", "--oversize-policy", "compress", "/videos/a.mp4"])

    assert default_args.oversize_policy == "error"
    assert split_args.oversize_policy == "split"
    assert compress_args.oversize_policy == "compress"
    assert "--auto-split" not in parser.format_help()
    assert "--auto-compress" not in parser.format_help()


def test_policy_specific_media_targets_have_exact_values() -> None:
    assert SAFE_UPLOAD_LIMIT_BYTES == 2_000_000_000
    assert SPLIT_MEDIA_TARGET_BYTES == 1_960_000_000
    assert COMPRESS_MEDIA_TARGET_BYTES == 1_900_000_000

    assert prepared_media_target_bytes("error") == SPLIT_MEDIA_TARGET_BYTES
    assert prepared_media_target_bytes("split") == SPLIT_MEDIA_TARGET_BYTES
    assert prepared_media_target_bytes("compress") == COMPRESS_MEDIA_TARGET_BYTES


def test_validate_paths_can_defer_oversized_files_for_preparation(tmp_path: Path) -> None:
    source = tmp_path / "large.mp4"
    with source.open("wb") as file:
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)

    assert validate_upload_paths([str(source)], allow_oversized=True) == [source]
    with pytest.raises(NonRetryableUploadError, match="too large"):
        validate_upload_paths([str(source)])


def test_media_group_payload_uses_local_file_uris_and_one_caption(tmp_path: Path) -> None:
    first = tmp_path / "part 0001.mp4"
    second = tmp_path / "part-0002.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    payload = build_media_group_payload(fake_profile(12345), [first, second], "original")

    assert payload["chat_id"] == "-1001234567890"
    assert payload["reply_parameters"] == {"message_id": 12345}
    media = payload["media"]
    assert isinstance(media, list)
    assert len(media) == 2
    assert media[0]["media"].startswith("file://")
    assert "%20" in media[0]["media"]
    assert media[0]["caption"] == "original"
    assert "caption" not in media[1]
    assert all(item["supports_streaming"] is True for item in media)


def test_direct_media_group_omits_reply_parameters(tmp_path: Path) -> None:
    first = tmp_path / "part-0001.mp4"
    second = tmp_path / "part-0002.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    payload = build_media_group_payload(fake_profile(None), [first, second], "")

    assert "reply_parameters" not in payload


@pytest.mark.parametrize(
    ("count", "expected_sizes"),
    [
        (2, [2]),
        (10, [10]),
        (11, [6, 5]),
        (20, [10, 10]),
        (21, [7, 7, 7]),
    ],
)
def test_media_group_partition_is_minimal_balanced_and_never_has_a_singleton(
    count: int,
    expected_sizes: list[int],
) -> None:
    paths = [Path(f"/videos/part-{index:04d}.mp4") for index in range(count)]

    groups = partition_media_groups(paths)

    assert [len(group) for group in groups] == expected_sizes
    assert [path for group in groups for path in group] == paths
    assert all(2 <= len(group) <= 10 for group in groups)


def test_send_media_group_validates_expected_result_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [tmp_path / "part-0001.mp4", tmp_path / "part-0002.mp4"]
    for part in parts:
        part.write_bytes(b"video")
    captured: dict[str, Any] = {}

    def fake_post_json(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "ok": True,
            "result": [{"message_id": 1}, {"message_id": 2}],
        }

    monkeypatch.setattr("tg_comment_uploader.cli.post_json", fake_post_json)

    result = send_media_group(fake_config(), fake_profile(), parts, "caption")

    assert result == [{"message_id": 1}, {"message_id": 2}]
    assert captured["method"] == "sendMediaGroup"
    assert captured["file_path"] == parts[0]
    assert captured["payload"]["media"][0]["caption"] == "caption"


def test_send_media_group_treats_malformed_success_as_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [tmp_path / "part-0001.mp4", tmp_path / "part-0002.mp4"]
    for part in parts:
        part.write_bytes(b"video")

    monkeypatch.setattr(
        "tg_comment_uploader.cli.post_json",
        lambda **kwargs: {"ok": True, "result": [{"message_id": 1}]},
    )

    with pytest.raises(NonRetryableUploadError, match="outcome is uncertain"):
        send_media_group(fake_config(), fake_profile(), parts, "caption")


def test_media_group_retry_reuses_same_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = [tmp_path / "part-0001.mp4", tmp_path / "part-0002.mp4"]
    for part in parts:
        part.write_bytes(b"video")
    calls: list[tuple[Path, ...]] = []

    def fake_send_media_group(
        config: AppConfig,
        profile: ProfileConfig,
        paths: tuple[Path, ...],
        caption: str,
    ) -> list[dict[str, Any]]:
        calls.append(tuple(paths))
        if len(calls) == 1:
            raise RetryableUploadError("temporary")
        return [{"message_id": 1}, {"message_id": 2}]

    monkeypatch.setattr("tg_comment_uploader.cli.send_media_group", fake_send_media_group)

    result = upload_media_groups_with_retries(
        fake_config(),
        fake_profile(),
        parts,
        "caption",
        source=tmp_path / "source.mp4",
        retries=1,
    )

    assert result == [{"message_id": 1}, {"message_id": 2}]
    assert calls == [tuple(parts), tuple(parts)]


def test_run_upload_locked_routes_split_outputs_to_media_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source video.mp4"
    with source.open("wb") as file:
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    parts = (tmp_path / "part-0001.mp4", tmp_path / "part-0002.mp4")
    for part in parts:
        part.write_bytes(b"video")
    events: list[str] = []

    @contextmanager
    def fake_prepare_media(
        path: Path,
        policy: str,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        assert path == source
        assert policy == "split"
        assert kwargs["hard_limit_bytes"] == SAFE_UPLOAD_LIMIT_BYTES
        assert kwargs["target_bytes"] == SPLIT_MEDIA_TARGET_BYTES
        events.append("prepared")
        yield PreparedMedia(source=source, paths=parts, policy="split")
        events.append("cleaned")

    def fake_send_media_group(
        config: AppConfig,
        profile: ProfileConfig,
        paths: tuple[Path, ...],
        caption: str,
    ) -> list[dict[str, Any]]:
        events.append("uploaded")
        assert tuple(paths) == parts
        assert caption == "source video"
        return [{"message_id": 1}, {"message_id": 2}]

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare_media)
    monkeypatch.setattr("tg_comment_uploader.cli.send_media_group", fake_send_media_group)

    args = build_parser().parse_args(["upload", "--oversize-policy", "split", str(source)])

    assert run_upload_locked(args) == 0
    assert events == ["prepared", "uploaded", "cleaned"]


def test_run_upload_locked_routes_compress_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    with source.open("wb") as file:
        file.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    compressed = tmp_path / "compressed.mp4"
    compressed.write_bytes(b"video")
    captured: dict[str, Any] = {}

    @contextmanager
    def fake_prepare_media(
        path: Path,
        policy: str,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        captured.update(kwargs)
        assert path == source
        assert policy == "compress"
        yield PreparedMedia(source=source, paths=(compressed,), policy="compress")

    def fake_upload(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["uploaded"] = True
        return {"message_id": 1}

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare_media)
    monkeypatch.setattr("tg_comment_uploader.cli.upload_with_retries", fake_upload)

    args = build_parser().parse_args(["upload", "--oversize-policy", "compress", str(source)])

    assert run_upload_locked(args) == 0
    assert captured["hard_limit_bytes"] == SAFE_UPLOAD_LIMIT_BYTES
    assert captured["target_bytes"] == COMPRESS_MEDIA_TARGET_BYTES
    assert captured["uploaded"] is True


class RecordingPreparationRenderer:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def update(self, label: str, fraction: float) -> None:
        self.events.append(("update", label, fraction))

    def log(self, message: str) -> None:
        self.events.append(("log", message))

    def finish(self) -> None:
        self.events.append(("finish",))


def test_split_progress_router_uses_stage_lines_and_structured_bar() -> None:
    renderer: Any = RecordingPreparationRenderer()

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


def test_compression_progress_router_uses_stage_lines_and_structured_bar() -> None:
    renderer: Any = RecordingPreparationRenderer()

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


def test_run_upload_routes_preparation_callbacks_and_finishes_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    renderer = RecordingPreparationRenderer()
    captured_callbacks: dict[str, Any] = {}

    @contextmanager
    def fake_prepare_media(
        path: Path,
        policy: str,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        assert path == source
        assert policy == "compress"
        captured_callbacks.update(kwargs)
        kwargs["progress"]("preparing workspace")
        kwargs["split_progress"](
            SplitProgress(
                stage="splitting",
                attempt=1,
                max_attempts=3,
                part_count=2,
                fraction=0.25,
            )
        )
        kwargs["compression_progress"](
            CompressionProgress(
                stage="compressing",
                attempt=1,
                max_attempts=3,
                pass_number=2,
                fraction=0.75,
            )
        )
        yield PreparedMedia(source=source, paths=(source,), policy="compress")

    monkeypatch.setattr("tg_comment_uploader.cli.TerminalProgress", lambda: renderer)
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare_media)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.upload_with_retries",
        lambda *args, **kwargs: {"message_id": 42},
    )

    args = build_parser().parse_args(["upload", "--oversize-policy", "compress", str(source)])

    assert run_upload_locked(args) == 0
    assert set(captured_callbacks) >= {
        "progress",
        "split_progress",
        "compression_progress",
        "warning",
    }
    assert ("log", "[1/1] preparing workspace") in renderer.events
    assert ("update", "[1/1] split attempt 1/3 (2 parts)", 0.25) in renderer.events
    assert ("update", "[1/1] compress attempt 1/3 pass 2/2", 0.75) in renderer.events
    assert renderer.events[-1] == ("finish",)


@pytest.mark.parametrize("failure_point", ["preparation", "upload"])
def test_run_upload_finishes_renderer_on_every_failure_path(
    failure_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    renderer = RecordingPreparationRenderer()

    @contextmanager
    def fake_prepare_media(
        path: Path,
        policy: str,
        **kwargs: Any,
    ) -> Iterator[PreparedMedia]:
        kwargs["compression_progress"](
            CompressionProgress(
                stage="compressing",
                attempt=1,
                max_attempts=3,
                pass_number=1,
                fraction=0.4,
            )
        )
        if failure_point == "preparation":
            raise MediaPreparationError("conversion failed")
        yield PreparedMedia(source=path, paths=(path,), policy="compress")

    def fake_upload(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NonRetryableUploadError("upload failed")

    monkeypatch.setattr("tg_comment_uploader.cli.TerminalProgress", lambda: renderer)
    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: fake_config())
    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare_media)
    monkeypatch.setattr("tg_comment_uploader.cli.upload_with_retries", fake_upload)

    args = build_parser().parse_args(["upload", "--oversize-policy", "compress", str(source)])

    with pytest.raises(NonRetryableUploadError):
        run_upload_locked(args)

    assert renderer.events[-1] == ("finish",)
    assert renderer.events.count(("finish",)) == 1
