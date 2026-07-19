from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from tg_comment_uploader.cli import (
    SAFE_UPLOAD_LIMIT_BYTES,
    AppConfig,
    AppError,
    BotConfig,
    ProfileConfig,
    build_parser,
    run_upload_locked,
)
from tg_comment_uploader.errors import RetryableUploadError
from tg_comment_uploader.media_workflow import PreparedMedia
from tg_comment_uploader.upload_state import (
    FileIdentity,
    PendingOwner,
    PendingUploadStore,
    SourceIntent,
)


class AlbumSender:
    def __init__(
        self,
        *,
        fail_call: int | None = None,
        first_message_id: int = 1000,
    ) -> None:
        self.fail_call = fail_call
        self.first_message_id = first_message_id
        self.calls: list[tuple[tuple[Path, ...], tuple[int, ...]]] = []
        self.closed = False

    def send_media_group(
        self,
        items: Any,
        *,
        random_ids: Any,
        before_final_request: Any,
        **kwargs: Any,
    ) -> tuple[int, ...]:
        paths = tuple(item.path for item in items)
        ids = tuple(random_ids)
        self.calls.append((paths, ids))
        before_final_request()
        if self.fail_call == len(self.calls):
            raise RetryableUploadError(
                "response lost after final send",
                outcome_uncertain=True,
                final_request_started=True,
            )
        start = self.first_message_id + sum(len(previous[0]) for previous in self.calls[:-1])
        return tuple(range(start, start + len(paths)))

    def send_video(self, *args: Any, **kwargs: Any) -> int:
        raise AssertionError("single send was not expected")

    def close(self) -> None:
        self.closed = True


def test_restart_skips_confirmed_first_group_and_reuses_second_group_random_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "large.mp4"
    with source.open("wb") as output:
        output.truncate(SAFE_UPLOAD_LIMIT_BYTES + 1)
    parts = tuple(tmp_path / f"large part-{index:04d}.mp4" for index in range(1, 12))
    for index, part in enumerate(parts):
        part.write_bytes(f"part-{index}".encode())

    profile = ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=42,
        caption="{stem}",
        supports_streaming=True,
    )
    config = AppConfig(
        bot=BotConfig(token="123456:TEST", api_id=1, api_hash="hash"),
        profiles={"default": profile},
    )
    owner = PendingOwner(config_fingerprint="c" * 64, bot_id=123456)
    state = PendingUploadStore(tmp_path / "pending.json", owner=owner)
    paths = SimpleNamespace(session_path=tmp_path / "session.session")
    first_sender = AlbumSender(fail_call=2)
    second_sender = AlbumSender(first_message_id=2000)
    sender_queue = [first_sender, second_sender]

    monkeypatch.setattr("tg_comment_uploader.cli.load_config", lambda path: config)
    monkeypatch.setattr(
        "tg_comment_uploader.cli.create_pending_store",
        lambda config_path, bot: (paths, state),
    )
    monkeypatch.setattr(
        "tg_comment_uploader.cli.fingerprint_sources",
        lambda files, captions, allow_oversized: (
            SourceIntent(
                FileIdentity(str(source), SAFE_UPLOAD_LIMIT_BYTES + 1, "a" * 64),
                "large",
            ),
        ),
    )

    @contextmanager
    def fake_prepare(path: Path, policy: str, **kwargs: Any) -> Iterator[PreparedMedia]:
        yield PreparedMedia(source=source, paths=parts, policy="split")

    monkeypatch.setattr("tg_comment_uploader.cli.prepare_media", fake_prepare)

    def create_sender(*args: Any, **kwargs: Any) -> AlbumSender:
        kwargs["peer_resolved"](-1001234567890)
        return sender_queue.pop(0)

    monkeypatch.setattr("tg_comment_uploader.cli.create_sender", create_sender)
    args = build_parser().parse_args(["upload", "-o", "split", "--retries", "0", str(source)])

    with pytest.raises(AppError, match="failed after 1 attempts"):
        run_upload_locked(args)

    checkpoint = state.inspect()
    assert checkpoint is not None
    assert [unit.status for unit in checkpoint.units] == ["confirmed", "sending"]
    assert [len(call[0]) for call in first_sender.calls] == [6, 5]
    second_group_random_ids = first_sender.calls[1][1]

    assert run_upload_locked(args) == 0

    assert len(second_sender.calls) == 1
    assert second_sender.calls[0][0] == parts[6:]
    assert second_sender.calls[0][1] == second_group_random_ids
    assert state.inspect() is None
    output = capsys.readouterr()
    assert "restored media group 1/2" in output.out
    assert "may duplicate" not in output.err
    assert first_sender.closed is True
    assert second_sender.closed is True
