from __future__ import annotations

from io import StringIO

import pytest

from tg_comment_uploader.strict_json import (
    DuplicateJsonKeyError,
    load_strict_json,
    loads_strict_json,
)


@pytest.mark.parametrize(
    "payload",
    [
        '{"value": 1, "value": 2}',
        '{"outer": {"value": 1, "value": 2}}',
        '{"items": [{"value": 1, "value": 2}]}',
    ],
)
def test_strict_json_rejects_duplicate_keys_at_every_depth(payload: str) -> None:
    with pytest.raises(DuplicateJsonKeyError, match="duplicate JSON object key 'value'"):
        loads_strict_json(payload)


def test_stream_and_string_loaders_share_strict_object_rules() -> None:
    payload = '{"first": 1, "nested": {"second": 2}}'

    assert load_strict_json(StringIO(payload)) == loads_strict_json(payload)
