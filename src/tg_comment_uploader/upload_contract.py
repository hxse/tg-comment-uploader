"""Shared invariants for planning and sending one Telegram upload."""

from __future__ import annotations

from typing import Literal

OversizePolicy = Literal["error", "split", "compress"]
OVERSIZE_POLICIES: tuple[OversizePolicy, ...] = ("error", "split", "compress")

MEDIA_GROUP_MIN_ITEMS = 2
MEDIA_GROUP_MAX_ITEMS = 10
