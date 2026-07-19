"""Small synchronous interface between the CLI and a Telegram transport."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

from .errors import NonRetryableUploadError

UploadProgress = Callable[[int, int], None]
MediaGroupUploadProgress = Callable[[int, int, int], None]
FinalRequestStatus = Callable[[bool], None]
PeerResolved = Callable[[int], None]
BeforeFinalRequest = Callable[[], None]

SAFE_UPLOAD_LIMIT_BYTES = 2_000_000_000
TELEGRAM_INT32_MAX = 2**31 - 1
OVERSIZE_POLICY_HINT = (
    "Use --oversize-policy split for lossless splitting, or "
    "--oversize-policy compress for lossy compression."
)


def parse_bot_id(bot_token: object) -> int:
    """Extract the positive bot ID prefix shared by state and transport code."""

    if not isinstance(bot_token, str):
        raise ValueError("must be a string")
    prefix, separator, _ = bot_token.partition(":")
    if separator != ":" or not prefix.isascii() or not prefix.isdecimal():
        raise ValueError("does not contain a valid bot ID prefix")
    bot_id = int(prefix)
    if bot_id <= 0:
        raise ValueError("contains a non-positive bot ID")
    return bot_id


@dataclass(frozen=True)
class UploadItem:
    """One local file and its already-rendered Telegram caption."""

    path: Path
    caption: str
    expected_size: int
    expected_sha256: str


def validate_upload_file_size(path: Path, size: int) -> None:
    """Apply the one conservative Telegram size threshold used everywhere."""

    if size > SAFE_UPLOAD_LIMIT_BYTES:
        raise NonRetryableUploadError(
            f"upload file is above this project's safe Telegram limit: {path}; "
            f"size={size:,} bytes, limit={SAFE_UPLOAD_LIMIT_BYTES:,} bytes. "
            f"{OVERSIZE_POLICY_HINT} No upload was attempted."
        )


class TelegramSender(Protocol):
    """Synchronous delivery facade used by the sequential CLI workflow."""

    def send_video(
        self,
        item: UploadItem,
        *,
        random_id: int,
        progress_callback: UploadProgress | None = None,
        before_final_request: BeforeFinalRequest | None = None,
    ) -> int:
        """Upload and send one video, returning its Telegram message ID."""

    def send_media_group(
        self,
        items: Sequence[UploadItem],
        *,
        random_ids: Sequence[int],
        progress_callback: MediaGroupUploadProgress | None = None,
        before_final_request: BeforeFinalRequest | None = None,
    ) -> tuple[int, ...]:
        """Upload and atomically send one album in input order."""

    def close(self) -> None:
        """Disconnect the underlying client and release its event loop."""

    def __enter__(self) -> TelegramSender:
        """Return the sender for use as a context manager."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the sender when leaving its context."""
