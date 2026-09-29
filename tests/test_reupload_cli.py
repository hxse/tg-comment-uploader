from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tg_comment_uploader.cli import build_parser, main
from tg_comment_uploader.config import AppConfig, ReuploadConfig, load_config
from tg_comment_uploader.locking import upload_instance_lock


def example() -> dict:
    return {
        "bot": {"api_id": 123, "api_hash": "sensitive-hash", "token": "123456:secret"},
        "profiles": {"default": {"chat_id": "-1000000000123", "caption": "{stem}"}},
    }


def test_legacy_config_still_works_and_reupload_has_its_own_defaults(tmp_path: Path) -> None:
    original = example()
    assert AppConfig.model_validate(original).reupload is None
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**original, "reupload": {"chat_id": -1000000000456}}))
    config = load_config(path)
    assert config.profiles["default"].chat_id == "-1000000000123"
    assert config.reupload is not None
    assert config.reupload.chat_id == "-1000000000456"
    assert config.reupload.keep_downloads is False
    assert config.reupload.retries == 5
    assert build_parser().parse_args(["reupload", "--status"]).status is True


@pytest.mark.parametrize(
    "options",
    [
        {"chat_id": True},
        {"chat_id": 0},
        {"chat_id": "@name"},
        {"retries": -1},
        {"keep_downloads": "yes"},
        {"mode": "copy"},
        {"download_dir": ""},
        {"download_dir": True},
    ],
)
def test_reupload_config_is_strict_and_does_not_reflect_credentials(options: dict) -> None:
    with pytest.raises(ValidationError) as caught:
        AppConfig.model_validate({**example(), "reupload": options})
    assert "sensitive-hash" not in str(caught.value)
    assert "123456:secret" not in str(caught.value)


def test_reupload_requires_an_explicit_target_before_network(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(example()))
    assert main(["reupload", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert "reupload.chat_id" in output.err
    assert "sensitive-hash" not in output.err
    assert ReuploadConfig(chat_id=None).chat_id is None


def test_both_commands_hold_the_same_project_lock(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "justfile").touch()
    monkeypatch.setattr(
        "tg_comment_uploader.cli.upload_instance_lock", lambda: upload_instance_lock(tmp_path)
    )
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.upload_instance_lock",
        lambda: upload_instance_lock(tmp_path),
    )
    with upload_instance_lock(tmp_path):
        assert main(["reupload"]) == 1
        assert main(["upload", "/unused/file.mp4"]) == 1
    assert capsys.readouterr().err.count("upload or reupload command is already running") == 2


def test_status_is_offline_and_does_not_create_a_bot_session(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from tg_comment_uploader.upload_state import get_mtproto_paths

    (tmp_path / "justfile").touch()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**example(), "reupload": {"chat_id": "-1000000000123"}}))
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.get_mtproto_paths",
        lambda *a, **kw: get_mtproto_paths(*a, **kw, project_root=tmp_path),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not create a sender")

    monkeypatch.setattr("tg_comment_uploader.reupload_cli.MtprotoSender", forbidden)
    assert main(["reupload", "--config", str(config), "--status"]) == 0
    output = capsys.readouterr().out
    assert "0 confirmed, 0 pending" in output
    assert "sensitive-hash" not in output and "123456:secret" not in output
    assert not (tmp_path / ".local" / "tg-comment-uploader" / "mtproto" / "sessions").exists()


def test_status_shows_override_without_moving_existing_downloads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from reupload_fakes import CHAT, forwarded, queue_at
    from tg_comment_uploader.reupload_cli import run_reupload_locked

    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    old_bytes = queue.path.read_bytes()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**example(), "reupload": {"chat_id": CHAT}}))
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.ReuploadQueue", lambda *a, **kw: queue)
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.find_project_root", lambda: tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not move downloads or create a sender")

    monkeypatch.setattr("tg_comment_uploader.reupload_cli.relocate_downloads", forbidden)
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.MtprotoSender", forbidden)
    target = tmp_path / "new downloads"
    args = build_parser().parse_args(
        ["reupload", "--config", str(config), "--download-dir", str(target), "--status"]
    )
    assert run_reupload_locked(args) == 0
    output = capsys.readouterr().out
    assert f"downloads: {target}" in output
    assert f"stored downloads: {queue.state.download_root}" in output
    assert "migration pending" in output
    assert queue.path.read_bytes() == old_bytes
    assert not target.exists()
