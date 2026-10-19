from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from tg_comment_uploader.cli import build_parser, main
from tg_comment_uploader.config import AppConfig, ReuploadConfig, load_config
from tg_comment_uploader.locking import upload_instance_lock


@pytest.fixture(autouse=True)
def isolated_project_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "justfile").touch()
    for module in ("cli", "reupload_cli"):
        monkeypatch.setattr(
            f"tg_comment_uploader.{module}.upload_instance_lock",
            lambda: upload_instance_lock(tmp_path),
        )


def example() -> str:
    return """
[bot]
api_id = 123
api_hash = "sensitive-hash"
token = "123456:secret"
[profiles.default]
chat_id = "-1000000000123"
caption = "{stem}"
"""


def test_config_without_reupload_works_and_reupload_has_its_own_defaults(tmp_path: Path) -> None:
    original = example()
    assert AppConfig.model_validate(tomllib.loads(original)).reupload is None
    path = tmp_path / "config.toml"
    path.write_text(original + "\n[reupload]\nchat_id = -1000000000456\n", encoding="utf-8")
    config = load_config(path)
    assert config.profiles["default"].chat_id == "-1000000000123"
    assert config.reupload is not None
    assert config.reupload.chat_id == "-1000000000456"
    assert config.reupload.keep_downloads is False
    assert config.reupload.retries == 5
    args = build_parser().parse_args(["reupload", "--status"])
    assert args.status is True
    assert args.config == Path("config.toml")


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
        AppConfig.model_validate({**tomllib.loads(example()), "reupload": options})
    assert "sensitive-hash" not in str(caught.value)
    assert "123456:secret" not in str(caught.value)


def test_reupload_requires_an_explicit_target_before_network(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.toml"
    path.write_text(example(), encoding="utf-8")
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
    config = tmp_path / "config.toml"
    config.write_text(example() + '\n[reupload]\nchat_id = "-1000000000123"\n', encoding="utf-8")
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


def test_root_toml_status_reads_existing_legacy_queue(tmp_path: Path, monkeypatch, capsys) -> None:
    from reupload_fakes import CHAT, forwarded
    from tg_comment_uploader.reupload_queue import ReuploadQueue
    from tg_comment_uploader.upload_state import get_mtproto_paths

    legacy = tmp_path / "config/config.json"
    legacy.parent.mkdir()
    legacy.touch()
    paths = get_mtproto_paths(
        legacy,
        "123456:secret",
        api_id=123,
        api_hash="sensitive-hash",
        project_root=tmp_path,
    )
    owner = f"{paths.owner.config_fingerprint}:bot{paths.owner.bot_id}"
    namespace = hashlib.sha256(f"{owner}:{CHAT}".encode()).hexdigest()[:32]
    queue = ReuploadQueue(
        paths.pending_path.parent / "reupload" / namespace / "queue-v1.json",
        owner=owner,
        chat_id=CHAT,
        download_root=tmp_path / "downloads",
    )
    queue.enqueue(forwarded(1))
    before = queue.path.read_bytes()
    legacy.unlink()
    config = tmp_path / "config.toml"
    config.write_text(example() + f"\n[reupload]\nchat_id = {CHAT}\n", encoding="utf-8")
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.get_mtproto_paths",
        lambda *a, **kw: get_mtproto_paths(*a, **kw, project_root=tmp_path),
    )

    assert main(["reupload", "--config", str(config), "--status"]) == 0
    assert "0 confirmed, 1 pending" in capsys.readouterr().out
    assert queue.path.read_bytes() == before


def test_status_shows_override_without_moving_existing_downloads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from reupload_fakes import CHAT, forwarded, queue_at
    from tg_comment_uploader.reupload_cli import run_reupload_locked

    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    old_bytes = queue.path.read_bytes()
    config = tmp_path / "config.toml"
    config.write_text(example() + f"\n[reupload]\nchat_id = {CHAT}\n", encoding="utf-8")
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
