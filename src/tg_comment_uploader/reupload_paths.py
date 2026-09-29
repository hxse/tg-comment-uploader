"""Resolve download roots without sourcing shell configuration or extra packages."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def system_downloads_directory() -> Path:
    home = Path.home()
    fallback = home / "Downloads"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = home / ".config"
    try:
        lines = (config_home / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return fallback
    for line in lines:
        match = re.fullmatch(r"\s*XDG_DOWNLOAD_DIR\s*=\s*(.*)", line)
        if match is None:
            continue
        try:
            values = shlex.split(match[1], comments=True)
        except ValueError:
            continue
        if len(values) != 1:
            continue
        value = values[0]
        for prefix in ("$HOME", "${HOME}"):
            if value == prefix or value.startswith(prefix + "/"):
                value = str(home) + value[len(prefix) :]
                break
        directory = Path(value)
        if directory.is_absolute():
            return directory
    return fallback


def resolve_download_root(
    override: Path | None, configured: str | None, *, project_root: Path
) -> Path:
    if override is not None:
        directory = override
    elif configured is not None:
        directory = Path(configured)
    else:
        directory = system_downloads_directory() / "tg-comment-uploader" / "reupload"
    directory = directory.expanduser()
    if not directory.is_absolute():
        directory = project_root / directory
    return directory.resolve()
