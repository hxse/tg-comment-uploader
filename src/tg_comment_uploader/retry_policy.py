"""Backoff used by synchronous uploads and the asynchronous backup worker."""

from .errors import RetryableUploadError

RETRY_BACKOFF_INITIAL_SECONDS = 1
RETRY_BACKOFF_MAX_SECONDS = 30
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60


def retry_delay_seconds(error: RetryableUploadError, *, failed_attempt: int) -> int:
    if error.retry_after_seconds is not None:
        return min(max(error.retry_after_seconds, 1), MAX_RETRY_AFTER_SECONDS)

    exponent = min(max(failed_attempt - 1, 0), 30)
    delay = RETRY_BACKOFF_INITIAL_SECONDS * (1 << exponent)
    return min(delay, RETRY_BACKOFF_MAX_SECONDS)
