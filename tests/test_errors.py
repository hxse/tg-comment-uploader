from __future__ import annotations

from tg_comment_uploader.errors import (
    AppError,
    MAX_UNTRUSTED_ERROR_TEXT_LENGTH,
    RetryableUploadError,
    TelegramUploadError,
    sanitize_untrusted_error_text,
)


def test_upload_error_exposes_retry_metadata() -> None:
    error = RetryableUploadError(
        "later",
        retry_after_seconds=17,
        outcome_uncertain=True,
        final_request_started=True,
    )

    assert isinstance(error, TelegramUploadError)
    assert isinstance(error, AppError)
    assert error.retry_after_seconds == 17
    assert error.outcome_uncertain is True
    assert error.final_request_started is True


def test_sanitize_untrusted_error_text_redacts_escapes_and_truncates() -> None:
    token = "123456:super-secret-token"
    rendered = sanitize_untrusted_error_text(
        f"token={token}\n/botother-secret/send {('x' * 600)}",
        secrets=(token,),
    )

    assert token not in rendered
    assert "other-secret" not in rendered
    assert "<redacted>" in rendered
    assert "\\n" in rendered
    assert "\n" not in rendered
    assert rendered.endswith("...<truncated>")
    assert len(rendered) == MAX_UNTRUSTED_ERROR_TEXT_LENGTH
