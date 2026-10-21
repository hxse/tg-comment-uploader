from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, CHAT, NOW, FakeBot, forwarded, queue_at, sender_at
from test_reupload_discard import cache_files, configure_cli
from tg_comment_uploader.cli import build_parser, main
from tg_comment_uploader.config import AppConfig
from tg_comment_uploader.errors import RetryableUploadError
from tg_comment_uploader.reupload_cli import run_reupload_locked
from tg_comment_uploader.reupload_service import ReuploadService


@pytest.mark.parametrize("command", ["reupload", "download"])
def test_resume_is_the_default_and_fresh_is_explicit(command: str) -> None:
    parser = build_parser()
    assert parser.parse_args([command]).fresh is False
    assert parser.parse_args([command, "--fresh"]).fresh is True


@pytest.mark.parametrize("command", ["reupload", "download"])
@pytest.mark.parametrize("flag", ["--resume", "--no-resume", "--discard-pending"])
def test_removed_resume_flags_are_rejected(command: str, flag: str) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args([command, flag])
    assert caught.value.code == 2


@pytest.mark.parametrize("status", ["queued", "downloading", "ready", "sending"])
def test_restart_resumes_old_jobs_in_order_with_the_same_random_ids(
    tmp_path: Path, status: str
) -> None:
    queue = queue_at(tmp_path)
    message = forwarded(1)
    queue.enqueue(message)
    job = queue.state.jobs[0]
    if status != "queued":
        queue.begin(job)
    if status == "ready":
        queue.ready(job.key)
    if status == "sending":
        queue.sending(job.key)
    before = queue.get(job.key)
    queue.enqueue(forwarded(2))
    saved_bytes = queue.path.read_bytes()

    restored = queue_at(tmp_path)
    pending = restored.next_job()
    assert pending == before
    assert pending is not None and pending.random_ids == job.random_ids
    assert not restored.enqueue(message)
    assert queue.path.read_bytes() == saved_bytes
    restored.begin(pending)
    restored.sending(pending.key)
    restored.confirm(pending.key, (100,))
    following = restored.next_job()
    assert following is not None and following.first_id == 2
    restored.begin(following)
    restored.sending(following.key)
    restored.confirm(following.key, (101,))
    assert queue_at(tmp_path).next_job() is None


@pytest.mark.parametrize("command", ["reupload", "download"])
@pytest.mark.parametrize("status", ["queued", "downloading", "ready", "sending"])
def test_fresh_permanently_abandons_old_jobs_without_deleting_files_or_confirmed_records(
    tmp_path: Path, monkeypatch, command: str, status: str
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))
    confirmed = queue.get("message-1")
    queue.enqueue(forwarded(2, kind="document"))
    queue.update(queue.get("message-2").model_copy(update={"status": status}))
    previous = queue.get("message-2")
    files = cache_files(queue, previous.key)
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    assert main([command, "--fresh"]) == 0
    abandoned = queue_at(tmp_path).get(previous.key)
    assert abandoned.status == "abandoned" and abandoned.abandoned_from == status
    assert abandoned.messages == previous.messages
    assert (files / "media.bin").read_bytes() == b"completed bytes"
    assert queue.get("message-1") == confirmed
    assert main([command]) == 0
    assert len(listeners) == 2
    assert queue_at(tmp_path).get(previous.key) == abandoned
    assert queue.next_job() is None


def test_fresh_ignores_old_offline_updates_and_keeps_new_work_after_restart(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    old = forwarded(1, kind="document")
    queue.enqueue(old)
    old_job = queue.state.jobs[0]
    queue.discard_pending(now=int(NOW.timestamp()))
    abandoned = queue.get(old_job.key)
    offline = forwarded(2)
    offline.date = NOW - timedelta(seconds=1)
    fresh = forwarded(3, kind="photo")
    fresh.date = NOW + timedelta(seconds=1)
    assert fresh.fwd_from is not None
    fresh.fwd_from.date = NOW - timedelta(days=365)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)
    bot = FakeBot([fresh])
    with sender_at(tmp_path, bot) as sender:

        async def scenario() -> None:
            client, peer = await sender.connect()
            await service.on_message(SimpleNamespace(message=old))
            await service.on_message(SimpleNamespace(message=offline))
            assert queue.next_job() is None
            await service.on_message(SimpleNamespace(message=fresh))
            job = queue.next_job()
            assert job is not None and job.first_id == 3
            await service.process_job(sender, client, peer, job)

        sender.run_service(scenario())
    assert [n for kind, n in bot.timeline if kind == "download"] == [3]
    restored = queue_at(tmp_path)
    assert [j.first_id for j in restored.state.jobs] == [1, 3]
    assert restored.get(old_job.key) == abandoned
    assert restored.get(old_job.key).random_ids == old_job.random_ids
    assert restored.next_job() is None
    assert len(bot.delivered) == 1


def test_default_listener_accepts_retained_offline_updates(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)
    old = forwarded(1)
    old.date = NOW - timedelta(days=1)
    asyncio.run(service.on_message(SimpleNamespace(message=old)))
    pending = queue.next_job()
    assert pending is not None and pending.first_id == 1


@pytest.mark.parametrize("command", ["reupload", "download"])
def test_fresh_runs_once_and_preserves_new_jobs_and_cutoff_across_reconnects(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    config = AppConfig.model_validate(
        {
            "bot": {"token": f"{BOT}:fake-token", "api_id": 123, "api_hash": "fake-api-hash"},
            "profiles": {},
            "reupload": {"chat_id": CHAT, "retries": 1},
        }
    )
    paths = SimpleNamespace(
        pending_path=tmp_path / "pending.json",
        session_path=tmp_path / "fake.session",
        owner=SimpleNamespace(config_fingerprint="test-owner", bot_id=BOT),
    )
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.load_config", lambda path: config)
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.get_mtproto_paths", lambda *a, **kw: paths
    )
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.ReuploadQueue", lambda *a, **kw: queue)
    monkeypatch.setattr("tg_comment_uploader.reupload_maintenance.time.time", NOW.timestamp)
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.time.sleep", lambda delay: None)
    cutoffs: list[int] = []

    class ReconnectingSender:
        def __init__(self, **kwargs):
            self.service = kwargs["client_factory"].__self__

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def run_service(self, operation):
            operation.close()
            cutoffs.append(self.service.queue.state.discarded_through)
            if len(cutoffs) == 1:
                assert queue.next_job() is None
                fresh = forwarded(2)
                fresh.date = NOW + timedelta(seconds=1)
                assert queue.enqueue(fresh)
                monkeypatch.setattr(
                    "tg_comment_uploader.reupload_maintenance.time.time",
                    lambda: NOW.timestamp() + 60,
                )
                raise RetryableUploadError("test disconnect")
            pending = queue.next_job()
            assert pending is not None and pending.first_id == 2
            offline = forwarded(3)
            offline.date = NOW + timedelta(seconds=30)
            assert queue.enqueue(offline)

    monkeypatch.setattr("tg_comment_uploader.reupload_cli.MtprotoSender", ReconnectingSender)
    args = build_parser().parse_args(
        [command, "--fresh", "--download-dir", str(tmp_path / "downloads")]
    )
    assert run_reupload_locked(args) == 0
    assert cutoffs == [int(NOW.timestamp())] * 2
    restored = queue_at(tmp_path)
    assert [job.status for job in restored.state.jobs] == ["abandoned", "queued", "queued"]
