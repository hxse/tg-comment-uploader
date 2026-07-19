"""JSON decoding that rejects duplicate object member names at every depth."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, TextIO


class StrictJsonError(ValueError):
    """Raised when JSON syntax or object member identity is invalid."""


class DuplicateJsonKeyError(StrictJsonError):
    """Raised when an object repeats a member name."""


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def loads_strict_json(payload: str) -> Any:
    """Decode one JSON string while rejecting duplicate keys recursively."""

    try:
        return json.loads(payload, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StrictJsonError(str(exc)) from exc


def load_strict_json(input_file: TextIO) -> Any:
    """Decode one JSON text stream while rejecting duplicate keys recursively."""

    try:
        return json.load(input_file, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StrictJsonError(str(exc)) from exc
