"""Shared strict configuration for upload and reupload commands."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import AppError, NonRetryableUploadError
from .mtproto_protocol import normalize_chat_id
from .strict_json import StrictJsonError, loads_strict_json
from .telegram_sender import TELEGRAM_INT32_MAX, parse_bot_id


class StrictConfigModel(BaseModel):
    """Immutable, non-coercing base for every layer of the config schema."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class BotConfig(StrictConfigModel):
    token: str = Field(min_length=1)
    api_id: int = Field(gt=0, le=TELEGRAM_INT32_MAX)
    api_hash: str = Field(min_length=1)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        try:
            parse_bot_id(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class ProfileConfig(StrictConfigModel):
    chat_id: str
    reply_message_id: int | None = Field(default=None, gt=0, le=TELEGRAM_INT32_MAX)
    caption: str = "{stem}"
    supports_streaming: bool = True

    @field_validator("chat_id", mode="before")
    @classmethod
    def validate_chat_id(cls, value: object) -> object:
        # Numeric IDs are an intentional part of the CLI contract. Store them
        # canonically as strings so pending-operation identity stays stable.
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("chat_id is invalid: chat_id must be a string or integer")
        candidate = str(value) if isinstance(value, int) else value
        try:
            normalize_chat_id(candidate)
        except NonRetryableUploadError as exc:
            raise ValueError(f"chat_id is invalid: {exc}") from exc
        return candidate

    @field_validator("caption", mode="before")
    @classmethod
    def normalize_null_caption(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("caption")
    @classmethod
    def validate_caption(cls, value: str) -> str:
        try:
            validate_caption_template_shape(value, context="caption")
        except AppError as exc:
            raise ValueError(str(exc)) from exc
        return value


class ReuploadConfig(StrictConfigModel):
    chat_id: str | None = None
    download_dir: str | None = Field(default=None, min_length=1)
    keep_downloads: bool = False
    retries: int = Field(default=5, ge=0)

    @field_validator("chat_id", mode="before")
    @classmethod
    def validate_chat_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("reupload.chat_id must be a numeric Telegram chat ID")
        try:
            normalized = normalize_chat_id(value)
        except NonRetryableUploadError as exc:
            raise ValueError(
                "reupload.chat_id must be a non-zero numeric Telegram chat ID"
            ) from exc
        if not isinstance(normalized, int):
            raise ValueError("reupload.chat_id must be a numeric ID, not a username or link")
        return str(normalized)


class AppConfig(StrictConfigModel):
    bot: BotConfig
    profiles: dict[str, ProfileConfig]
    reupload: ReuploadConfig | None = None

    @field_validator("profiles")
    @classmethod
    def validate_profile_names(
        cls,
        profiles: dict[str, ProfileConfig],
    ) -> dict[str, ProfileConfig]:
        if any(not name for name in profiles):
            raise ValueError("profile names must be non-empty strings")
        return profiles


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise AppError(f"config file does not exist: {path}")
    if not path.is_file():
        raise AppError(f"config path is not a file: {path}")

    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise AppError(f"failed to secure config file {path} with mode 0600: {exc}") from exc

    try:
        config_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AppError(f"failed to read config file {path}: {exc}") from exc

    try:
        raw = loads_strict_json(config_text)
    except StrictJsonError as exc:
        raise AppError(f"invalid JSON in {path}: {exc}") from exc

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        # Every nested model hides input values, so credentials cannot be
        # reflected by Pydantic's human-readable validation report.
        raise AppError(f"invalid config in {path}: {exc}") from exc


def validate_caption_template_shape(template: str, *, context: str) -> None:
    allowed = {"name", "stem", "suffix", "parent", "path"}
    index = 0
    while index < len(template):
        character = template[index]
        if character == "{":
            if index + 1 < len(template) and template[index + 1] == "{":
                index += 2
                continue
            closing = template.find("}", index + 1)
            if closing < 0:
                raise AppError(f"{context} is invalid: unmatched '{{' at position {index}")
            field_name = template[index + 1 : closing]
            if field_name not in allowed:
                raise AppError(
                    f"{context} contains unsupported placeholder {{{field_name}}}; "
                    f"allowed exact fields: {', '.join(sorted(allowed))}"
                )
            index = closing + 1
            continue
        if character == "}":
            if index + 1 < len(template) and template[index + 1] == "}":
                index += 2
                continue
            raise AppError(f"{context} is invalid: unmatched '}}' at position {index}")
        index += 1
