"""Translate MTProto failures without exposing authentication details."""

from __future__ import annotations

from collections.abc import Sequence

from telethon import errors

from .errors import (
    NonRetryableUploadError,
    RetryableUploadError,
    TelegramUploadError,
    sanitize_untrusted_error_text,
)


def _translate_exception(
    exc: BaseException,
    *,
    final_request_started: bool,
    secrets: Sequence[str],
    context: str,
) -> TelegramUploadError:
    if isinstance(exc, TelegramUploadError):
        return exc

    if (
        isinstance(exc, errors.UserMigrateError)
        and context == "connecting to Telegram"
        and not final_request_started
    ):
        new_dc = getattr(exc, "new_dc", None)
        dc_label = (
            f"DC {new_dc}"
            if isinstance(new_dc, int) and not isinstance(new_dc, bool) and new_dc > 0
            else "a different Telegram data center"
        )
        return NonRetryableUploadError(
            f"Telegram moved this bot session to {dc_label}. Telethon saved the new data "
            "center in the persistent session, but this login attempt ended before retrying "
            "there. Rerun the exact same upload command; do not discard the pending state. "
            "This failed attempt did not start a final Telegram send.",
            final_request_started=False,
        )

    detail = sanitize_untrusted_error_text(str(exc), secrets=secrets)
    message = f"{context} failed ({type(exc).__name__})"
    if detail:
        message = f"{message}: {detail}"

    if isinstance(exc, errors.RandomIdDuplicateError):
        return RetryableUploadError(
            "Telegram has already seen this persistent random ID and will not create a second "
            "message, but this response did not confirm the original message_id",
            outcome_uncertain=True,
            final_request_started=True,
        )

    if isinstance(
        exc,
        (
            errors.FilePart0MissingError,
            errors.FilePartMissingError,
            errors.FileReferenceEmptyError,
            errors.FileReferenceExpiredError,
        ),
    ):
        return RetryableUploadError(
            message,
            outcome_uncertain=False,
            final_request_started=final_request_started,
        )

    if isinstance(exc, errors.FloodError):
        seconds = getattr(exc, "seconds", None)
        retry_after = int(seconds) if isinstance(seconds, (int, float)) and seconds >= 0 else None
        return RetryableUploadError(
            message,
            retry_after_seconds=retry_after,
            outcome_uncertain=False,
            final_request_started=final_request_started,
        )

    if isinstance(exc, errors.RPCError):
        code = getattr(exc, "code", None)
        if isinstance(exc, (errors.ServerError, errors.TimedOutError)) or (
            isinstance(code, int) and (code >= 500 or code < 0)
        ):
            return RetryableUploadError(
                message,
                outcome_uncertain=final_request_started,
                final_request_started=final_request_started,
            )
        return NonRetryableUploadError(message, final_request_started=final_request_started)

    if isinstance(exc, (TimeoutError, ConnectionError, EOFError, OSError)):
        return RetryableUploadError(
            message,
            outcome_uncertain=final_request_started,
            final_request_started=final_request_started,
        )

    return NonRetryableUploadError(message, final_request_started=final_request_started)
