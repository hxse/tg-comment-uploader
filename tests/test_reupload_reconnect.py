from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, CHAT, FakeBot, forwarded, queue_at, sender_at
from tg_comment_uploader import reupload_cli as cli
from tg_comment_uploader.cli import build_parser
from tg_comment_uploader.config import AppConfig
from tg_comment_uploader.errors import AppError, RetryableUploadError
from tg_comment_uploader.reupload_service import ReuploadService


def install_reconnects(tmp_path, monkeypatch, lifetimes, *, retries=2):
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    config = AppConfig.model_validate(
        {
            "bot": {"token": f"{BOT}:fake", "api_id": 123, "api_hash": "fake"},
            "profiles": {},
            "reupload": {"chat_id": CHAT, "retries": retries},
        }
    )
    paths = SimpleNamespace(
        pending_path=tmp_path / "pending.json",
        session_path=tmp_path / "fake.session",
        owner=SimpleNamespace(config_fingerprint="test-owner", bot_id=BOT),
    )
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "get_mtproto_paths", lambda *args, **kwargs: paths)
    monkeypatch.setattr(cli, "ReuploadQueue", lambda *args, **kwargs: queue)
    waits = []
    monkeypatch.setattr(cli.time, "sleep", waits.append)
    runs = iter(lifetimes)

    class Sender:
        def __init__(self, **kwargs):
            self.service = kwargs["client_factory"].__self__

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run_service(self, operation):
            operation.close()
            lifetime = next(runs)
            if isinstance(lifetime, BaseException):
                raise lifetime
            if lifetime is not None:
                self.service.connected_seconds = lifetime
                raise RetryableUploadError("simulated disconnection")

    monkeypatch.setattr(cli, "MtprotoSender", Sender)
    args = build_parser().parse_args(["reupload", "--download-dir", str(tmp_path / "downloads")])
    return args, waits, queue


def test_stable_recovery_resets_count_and_backoff_across_repeated_outages(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    args, waits, queue = install_reconnects(tmp_path, monkeypatch, [0, 900, 0, 60, None])
    saved = queue.state.jobs[0]
    assert cli.run_reupload_locked(args) == 0
    assert waits == [1, 1, 2, 1]
    assert capsys.readouterr().out.count("reconnect retry count reset") == 2
    assert queue_at(tmp_path).state.jobs[0] == saved


@pytest.mark.parametrize("lifetimes", [[0, 0, 0], [1, 30, 59.9]])
def test_failed_connections_and_short_flaps_still_exhaust_consecutive_budget(
    tmp_path: Path, monkeypatch, lifetimes
) -> None:
    args, waits, queue = install_reconnects(tmp_path, monkeypatch, lifetimes)
    saved = queue.path.read_bytes()
    with pytest.raises(AppError, match="3 consecutive attempts"):
        cli.run_reupload_locked(args)
    assert waits == [1, 2]
    assert queue.path.read_bytes() == saved


def test_default_budget_and_zero_retries_remain_bounded(tmp_path: Path, monkeypatch) -> None:
    args, waits, _ = install_reconnects(tmp_path, monkeypatch, [0] * 6, retries=5)
    with pytest.raises(AppError, match="6 consecutive attempts"):
        cli.run_reupload_locked(args)
    assert waits == [1, 2, 4, 8, 16]
    args, waits, _ = install_reconnects(tmp_path, monkeypatch, [900], retries=0)
    with pytest.raises(AppError, match="1 consecutive attempts"):
        cli.run_reupload_locked(args)
    assert waits == []


@pytest.mark.parametrize("failure", [AppError("invalid input"), KeyboardInterrupt()])
def test_permanent_failure_and_user_interrupt_never_reconnect(
    tmp_path: Path, monkeypatch, failure
) -> None:
    args, waits, _ = install_reconnects(tmp_path, monkeypatch, [failure])
    with pytest.raises(type(failure)):
        cli.run_reupload_locked(args)
    assert waits == []


def test_connection_uptime_excludes_worker_cleanup(tmp_path: Path, monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_service.time",
        SimpleNamespace(time=time.time, monotonic=lambda: now[0]),
    )
    bot = FakeBot([])
    service = ReuploadService(queue_at(tmp_path), bot_id=BOT, retries=0)
    with sender_at(tmp_path, bot) as sender:

        async def scenario():
            entered = asyncio.Event()

            async def worker(*args):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    now[0] += 1000  # Cleanup must not add to connection uptime.

            monkeypatch.setattr(service, "work", worker)
            task = asyncio.create_task(service.run(sender))
            await asyncio.wait_for(entered.wait(), 3)
            now[0] += 900
            await bot.disconnect()
            with pytest.raises(RetryableUploadError, match="disconnected"):
                await task

        sender.run_service(scenario())
    assert service.connected_seconds == 900


def test_slow_failed_login_does_not_count_as_a_stable_connection(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_service.time",
        SimpleNamespace(time=time.time, monotonic=lambda: now[0]),
    )

    class BrokenBot(FakeBot):
        async def start(self, **kwargs):
            now[0] += 900
            raise ConnectionError("failed login")

    service = ReuploadService(queue_at(tmp_path), bot_id=BOT, retries=0)
    service.connected_seconds = 900  # Reusing a service must not inherit old uptime.
    with sender_at(tmp_path, BrokenBot()) as sender:
        with pytest.raises(RetryableUploadError):
            sender.run_service(service.run(sender))
    assert service.connected_seconds == 0
