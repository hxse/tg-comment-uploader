from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, CHAT, NOW, FakeBot, forwarded, queue_at, sender_at
from tg_comment_uploader.cli import build_parser
from tg_comment_uploader.config import AppConfig
from tg_comment_uploader.errors import RetryableUploadError
from tg_comment_uploader.reupload_cli import run_reupload_locked
from tg_comment_uploader.reupload_service import ReuploadService


def test_resume_is_enabled_by_default_and_can_be_disabled_explicitly() -> None:
    parser = build_parser()
    assert parser.parse_args(["reupload"]).resume is True
    assert parser.parse_args(["reupload", "--resume"]).resume is True
    assert parser.parse_args(["reupload", "--no-resume"]).resume is False


@pytest.mark.parametrize("status", ["queued", "downloading", "ready", "sending"])
def test_deferred_jobs_and_random_ids_survive_new_work_and_next_start(
    tmp_path: Path,
    status: str,
) -> None:
    queue = queue_at(tmp_path)
    message = forwarded(1)
    queue.enqueue(message)
    job = queue.next_job()
    assert job is not None
    if status != "queued":
        queue.begin(job)
    if status == "ready":
        queue.ready(job.key)
    if status == "sending":
        queue.sending(job.key)
    before = queue.get(job.key)
    saved_bytes = queue.path.read_bytes()
    partial_file = tmp_path / "downloads" / "partial.bin"
    partial_file.parent.mkdir(exist_ok=True)
    partial_file.write_bytes(b"keep old downloaded bytes")

    assert queue.defer_pending() == 1
    assert queue.path.read_bytes() == saved_bytes
    assert queue.next_job() is None
    assert not queue.enqueue(message)
    queue.enqueue(forwarded(2))
    current = queue.next_job()
    assert current is not None and current.first_id == 2
    queue.begin(current)
    queue.sending(current.key)
    queue.confirm(current.key, (100,))
    assert queue.next_job() is None
    assert queue.get(job.key) == before
    assert partial_file.read_bytes() == b"keep old downloaded bytes"

    restored = queue_at(tmp_path)
    pending = restored.next_job()
    assert pending == before  # Default startup includes the old task again.
    assert pending is not None and pending.random_ids == job.random_ids
    restored.begin(pending)
    restored.sending(pending.key)
    restored.confirm(pending.key, (101,))
    assert restored.next_job() is None


def test_late_member_of_a_deferred_album_does_not_start_or_change_it(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    for message_id in (1, 2):
        queue.enqueue(forwarded(message_id, kind="photo", group=7))
    job = queue.next_job()
    assert job is not None
    queue.begin(job)
    queue.defer_pending()
    saved_bytes = queue.path.read_bytes()
    assert not queue.enqueue(forwarded(3, kind="photo", group=7))
    assert queue.path.read_bytes() == saved_bytes
    assert queue.next_job() is None
    queue.enqueue(forwarded(4))
    current = queue.next_job()
    assert current is not None and current.first_id == 4


def test_no_resume_ignores_offline_updates_and_continues_new_forwarded_media(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    old = forwarded(1, kind="document")
    queue.enqueue(old)
    old_job = queue.next_job()
    assert old_job is not None
    queue.defer_pending()
    offline = forwarded(2)
    offline.date = NOW - timedelta(seconds=1)
    fresh = forwarded(3, kind="photo")
    fresh.date = NOW + timedelta(seconds=1)
    # An old source post forwarded now is still a new task in B.
    assert fresh.fwd_from is not None
    fresh.fwd_from.date = NOW - timedelta(days=365)
    service = ReuploadService(
        queue,
        bot_id=BOT,
        retries=0,
        keep_downloads=True,
        settle_seconds=0,
        new_messages_since=int(NOW.timestamp()),
    )
    bot = FakeBot([old, fresh])
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
    assert [j.first_id for j in queue.state.jobs] == [1, 3]
    assert queue.get(old_job.key) == old_job

    restored = queue_at(tmp_path)
    resumed = ReuploadService(restored, bot_id=BOT, retries=0, keep_downloads=True)
    bot = FakeBot([old])
    with sender_at(tmp_path, bot) as sender:

        async def resume() -> None:
            client, peer = await sender.connect()
            job = restored.next_job()
            assert job is not None and job.random_ids == old_job.random_ids
            # Continue the server's output IDs after the newer backup.
            bot.delivered[queue.state.jobs[1].random_ids[0]] = 10000
            await resumed.process_job(sender, client, peer, job)

        sender.run_service(resume())
    assert [n for kind, n in bot.timeline if kind == "download"] == [1]
    assert restored.next_job() is None


def test_default_listener_accepts_retained_offline_updates(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=True)
    old = forwarded(1)
    old.date = NOW - timedelta(days=1)
    asyncio.run(service.on_message(SimpleNamespace(message=old)))
    pending = queue.next_job()
    assert pending is not None and pending.first_id == 1


def test_no_resume_keeps_this_runs_jobs_and_cutoff_across_reconnects(
    tmp_path: Path, monkeypatch
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
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.time.time", lambda: NOW.timestamp())
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
            cutoffs.append(self.service.new_messages_since)
            if len(cutoffs) == 1:
                assert queue.next_job() is None
                queue.enqueue(forwarded(2))
                monkeypatch.setattr(
                    "tg_comment_uploader.reupload_cli.time.time", lambda: NOW.timestamp() + 60
                )
                raise RetryableUploadError("test disconnect")
            pending = queue.next_job()
            assert pending is not None and pending.first_id == 2

    monkeypatch.setattr("tg_comment_uploader.reupload_cli.MtprotoSender", ReconnectingSender)
    args = build_parser().parse_args(
        ["reupload", "--no-resume", "--download-dir", str(tmp_path / "downloads")]
    )
    assert run_reupload_locked(args) == 0
    assert cutoffs == [int(NOW.timestamp())] * 2
    assert queue_at(tmp_path).state.jobs[0].first_id == 1
