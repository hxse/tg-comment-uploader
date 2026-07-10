from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tg_comment_uploader.cli import (
    AppConfig,
    AppError,
    BotConfig,
    NonRetryableUploadError,
    ProfileConfig,
    ServerConfig,
    upload_media_groups_with_retries,
)


def test_later_media_group_failure_reports_already_sent_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProfileConfig(
        chat_id="-1001234567890",
        reply_message_id=12345,
        caption="{stem}",
        supports_streaming=True,
    )
    config = AppConfig(
        bot=BotConfig(token="token", api_id=1, api_hash="hash"),
        server=ServerConfig(
            host="127.0.0.1",
            port=48973,
            binary="telegram-bot-api",
            work_dir=Path("."),
        ),
        profiles={"default": profile},
    )
    paths = [Path(f"/videos/part-{index:04d}.mp4") for index in range(11)]
    calls = 0

    def fake_send_media_group(
        config: AppConfig,
        profile: ProfileConfig,
        group: tuple[Path, ...],
        caption: str,
    ) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise NonRetryableUploadError("second group rejected")
        return [{"message_id": index} for index, _ in enumerate(group, start=1)]

    monkeypatch.setattr("tg_comment_uploader.cli.send_media_group", fake_send_media_group)

    with pytest.raises(AppError) as exc_info:
        upload_media_groups_with_retries(
            config,
            profile,
            paths,
            "caption",
            source=Path("/videos/source.mp4"),
            retries=0,
        )

    message = str(exc_info.value)
    assert "partially succeeded" in message
    assert "6 split video message(s)" in message
    assert "rerunning the whole command may duplicate them" in message
