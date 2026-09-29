from __future__ import annotations

from pathlib import Path

import pytest

from tg_comment_uploader.cli import build_parser
from tg_comment_uploader.config import ReuploadConfig
from tg_comment_uploader.reupload_paths import resolve_download_root, system_downloads_directory


@pytest.fixture
def user_dirs(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (home / ".config").mkdir()
    return home / ".config" / "user-dirs.dirs"


@pytest.mark.parametrize(
    ("setting", "suffix"),
    [
        ('XDG_DOWNLOAD_DIR="$HOME/下载 文件"', "下载 文件"),
        ('XDG_DOWNLOAD_DIR="${HOME}/Downloads" # comment', "Downloads"),
        ('  XDG_DOWNLOAD_DIR = "$HOME"', ""),
    ],
)
def test_downloads_uses_system_setting(user_dirs: Path, setting: str, suffix: str) -> None:
    user_dirs.write_text('XDG_DOCUMENTS_DIR="$HOME/Documents"\n' + setting + "\n")
    assert system_downloads_directory() == Path.home() / suffix


def test_downloads_reads_absolute_path_from_custom_xdg_config(
    user_dirs: Path, tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "custom-config"
    config.mkdir()
    destination = tmp_path / "external downloads"
    (config / user_dirs.name).write_text(f'XDG_DOWNLOAD_DIR="{destination}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    assert system_downloads_directory() == destination


@pytest.mark.parametrize(
    "setting",
    [
        None,
        "",
        'XDG_DESKTOP_DIR="$HOME/Desktop"',
        'XDG_DOWNLOAD_DIR="unterminated',
        'XDG_DOWNLOAD_DIR="relative/path"',
        'XDG_DOWNLOAD_DIR="$UNKNOWN/Downloads"',
        "XDG_DOWNLOAD_DIR=one two",
    ],
)
def test_missing_or_invalid_setting_falls_back_to_home_downloads(
    user_dirs: Path, setting: str | None, monkeypatch
) -> None:
    if setting is not None:
        user_dirs.write_text(setting)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config-is-invalid")
    assert system_downloads_directory() == Path.home() / "Downloads"


def test_user_dirs_is_parsed_as_data_and_never_executed(user_dirs: Path, tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    user_dirs.write_text(f'touch "{marker}"\nXDG_DOWNLOAD_DIR="$(touch {marker})"\n')
    assert system_downloads_directory() == Path.home() / "Downloads"
    assert not marker.exists()


def test_cli_overrides_config_and_auto_root_without_creating_directories(
    user_dirs: Path, tmp_path: Path
) -> None:
    user_dirs.write_text('XDG_DOWNLOAD_DIR="$HOME/系统下载"\n')
    automatic = resolve_download_root(None, None, project_root=tmp_path)
    assert automatic == Path.home() / "系统下载" / "tg-comment-uploader" / "reupload"
    assert not automatic.exists()
    assert (
        resolve_download_root(None, "configured", project_root=tmp_path) == tmp_path / "configured"
    )
    parser = build_parser()
    options = parser.parse_args(["reupload", "--download-dir", "custom path"])
    assert resolve_download_root(options.download_dir, "configured", project_root=tmp_path) == (
        tmp_path / "custom path"
    )
    assert resolve_download_root(tmp_path / "absolute", "configured", project_root=tmp_path) == (
        tmp_path / "absolute"
    )


def test_custom_directory_expands_tilde(tmp_path: Path) -> None:
    assert (
        resolve_download_root(None, "~/Downloads/custom", project_root=tmp_path)
        == (Path.home() / "Downloads" / "custom").resolve()
    )


def test_omitted_and_null_config_both_select_automatic_directory() -> None:
    assert ReuploadConfig().download_dir is None
    assert ReuploadConfig(download_dir=None).download_dir is None
