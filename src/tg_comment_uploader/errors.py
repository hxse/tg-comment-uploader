"""Expected CLI failures and safe rendering of untrusted Telegram text."""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_UNTRUSTED_ERROR_TEXT_LENGTH = 500
BOT_TOKEN_PATH_PATTERN = re.compile(
    r"/bot[^/?#\s]+(?=/|[?#\s]|$)",
    re.IGNORECASE,
)


class AppError(Exception):
    """Expected CLI failure with a user-facing message."""


class TelegramUploadError(AppError):
    """Base class for expected Telegram delivery failures."""

    def __init__(self, message: str, *, final_request_started: bool = False) -> None:
        super().__init__(message)
        self.final_request_started = final_request_started


class RetryableUploadError(TelegramUploadError):
    """A transient failure which may succeed when retried with the same random ID."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
        outcome_uncertain: bool = False,
        final_request_started: bool = False,
    ) -> None:
        super().__init__(message, final_request_started=final_request_started)
        self.retry_after_seconds = retry_after_seconds
        self.outcome_uncertain = outcome_uncertain


class NonRetryableUploadError(TelegramUploadError):
    """A deterministic failure which repeating the same operation cannot fix."""


def sanitize_untrusted_error_text(
    text: str,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Redact known secrets, escape controls and bound terminal error text."""

    redacted = str(text)
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    redacted = BOT_TOKEN_PATH_PATTERN.sub("/bot<redacted>", redacted)
    escaped = "".join(
        character if character.isprintable() else ascii(character)[1:-1] for character in redacted
    )
    if len(escaped) <= MAX_UNTRUSTED_ERROR_TEXT_LENGTH:
        return escaped

    suffix = "...<truncated>"
    return escaped[: MAX_UNTRUSTED_ERROR_TEXT_LENGTH - len(suffix)] + suffix
