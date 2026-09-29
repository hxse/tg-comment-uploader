from __future__ import annotations

from pathlib import Path

import pytest

from reupload_fakes import BOT, FakeBot, forwarded, queue_at, sender_at
from test_reupload_delivery import process_all
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import cleanup_job, job_directory
from tg_comment_uploader.reupload_service import ReuploadService


def startup_only(tmp_path, queue, monkeypatch, *, keep=False):
    service = ReuploadService(queue, bot_id=BOT, retries=0, keep_downloads=keep)

    async def no_work(*args):
        return

    monkeypatch.setattr(service, "work", no_work)
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:
        sender.run_service(service.run(sender))
    assert not bot.requests


def test_default_service_removes_files_only_after_durable_confirmation(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))
    job = queue.state.jobs[0]
    bot = FakeBot()
    service = ReuploadService(queue, bot_id=BOT, retries=0)

    def sending(request):
        assert (job_directory(queue, job) / "1" / "media.bin").is_file()
        assert queue_at(tmp_path).get(job.key).status == "sending"

    bot.send_hook = sending
    with sender_at(tmp_path, bot) as sender:

        async def run():
            client, peer = await sender.connect()
            await service.process_job(sender, client, peer, job)

        sender.run_service(run())
    assert len(bot.delivered) == 1
    assert not job_directory(queue, job).exists()
    saved = queue_at(tmp_path).get(job.key)
    assert saved.status == "confirmed" and not saved.cleanup_pending
    assert not queue_at(tmp_path).enqueue(forwarded(1, kind="document"))


def test_cleanup_failure_is_retried_after_restart_without_uploading_again(
    tmp_path: Path, monkeypatch
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))
    job = queue.state.jobs[0]

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr("tg_comment_uploader.reupload_service.cleanup_job", fail)
        process_all(tmp_path, FakeBot(), queue, keep=False)
    restored = queue_at(tmp_path)
    assert restored.get(job.key).cleanup_pending
    assert job_directory(restored, job).exists()
    # This was already scheduled for cleanup by the previous run's policy.
    startup_only(tmp_path, restored, monkeypatch, keep=True)
    assert not job_directory(restored, job).exists()
    assert not queue_at(tmp_path).get(job.key).cleanup_pending


def test_kept_files_survive_a_later_default_startup(tmp_path: Path, monkeypatch) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))
    process_all(tmp_path, FakeBot(), queue, keep=True)
    job = queue.state.jobs[0]
    restored = queue_at(tmp_path)
    startup_only(tmp_path, restored, monkeypatch)
    assert (
        job_directory(restored, job) / "1" / "media.bin"
    ).read_bytes() == b"downloaded media bytes"
    assert not restored.get(job.key).cleanup_pending


def test_failed_confirmation_keeps_files_for_resume(tmp_path: Path, monkeypatch) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))

    def fail(*args, **kwargs):
        raise AppError("failed to save confirmation")

    monkeypatch.setattr(queue, "confirm", fail)
    with pytest.raises(AppError, match="failed to save confirmation"):
        process_all(tmp_path, FakeBot(), queue, keep=False)
    saved = queue_at(tmp_path).state.jobs[0]
    assert saved.status == "sending" and not saved.cleanup_pending
    assert (job_directory(queue, saved) / "1" / "media.bin").is_file()


def test_cleanup_sync_failure_keeps_the_saved_retry_intent(tmp_path: Path, monkeypatch) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,), cleanup_pending=True)
    job = queue.get("message-1")
    directory = job_directory(queue, job) / "1"
    directory.mkdir(parents=True)
    (directory / "media.bin").write_bytes(b"already uploaded")

    def fail(*args, **kwargs):
        raise OSError("directory sync failed")

    with monkeypatch.context() as scoped:
        scoped.setattr("tg_comment_uploader.reupload_download.fsync_directory", fail)
        with pytest.raises(OSError, match="directory sync failed"):
            cleanup_job(queue, job)
    restored = queue_at(tmp_path)
    assert restored.get(job.key).cleanup_pending
    assert not directory.exists()
    cleanup_job(restored, restored.get(job.key))
    assert not queue_at(tmp_path).get(job.key).cleanup_pending
