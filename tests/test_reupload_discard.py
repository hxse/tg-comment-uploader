from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, CHAT, NOW, FakeBot, forwarded, queue_at
from test_reupload_dedup import forwarded_again
from test_reupload_delivery import process_all
from tg_comment_uploader.cli import main
from tg_comment_uploader.config import AppConfig
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.locking import upload_instance_lock
from tg_comment_uploader.reupload_download import job_directory
from tg_comment_uploader.reupload_maintenance import delete_downloads, discard_pending


def cache_files(queue, key):
    job = queue.get(key)
    directory = job_directory(queue, job) / str(job.first_id)
    directory.mkdir(parents=True)
    (directory / "message.json").write_bytes(b"{}")
    (directory / "media.bin").write_bytes(b"completed bytes")
    (directory / "media.bin.part").write_bytes(b"partial bytes")
    return directory


@pytest.mark.parametrize("status", ["queued", "downloading", "ready", "sending"])
def test_discard_is_permanent_and_keeps_confirmed_records_and_random_ids(
    tmp_path: Path, monkeypatch, capsys, status: str
) -> None:
    monkeypatch.setattr("tg_comment_uploader.reupload_maintenance.time.time", NOW.timestamp)
    queue = queue_at(tmp_path)
    completed = forwarded(1, source_post=900)
    pending = forwarded(2, kind="document", source_post=901)
    queue.enqueue(completed)
    queue.confirm("message-1", (100,))
    queue.enqueue(pending)
    queue.update(queue.get("message-2").model_copy(update={"status": status}))
    files = cache_files(queue, "message-2")
    before = queue.state.jobs
    discard_pending(queue)
    assert "permanently abandoned 1" in capsys.readouterr().out
    restored = queue_at(tmp_path)
    assert restored.state.jobs[0] == before[0]
    abandoned = restored.state.jobs[1]
    assert abandoned.status == "abandoned" and abandoned.abandoned_from == status
    assert abandoned.messages == before[1].messages
    assert restored.next_job() is None
    assert restored.defer_pending() == 0
    assert not restored.enqueue(pending)
    assert (files / "media.bin").read_bytes() == b"completed bytes"
    # Future forwards are new requests, whether the old input was confirmed or abandoned.
    assert restored.enqueue(forwarded_again(completed, 3))
    assert restored.enqueue(forwarded_again(pending, 4))
    bot = FakeBot()
    process_all(tmp_path, bot, restored)
    assert [j.status for j in restored.state.jobs] == [
        "confirmed",
        "abandoned",
        "confirmed",
        "confirmed",
    ]
    assert len(bot.delivered) == 2


def test_discard_cutoff_blocks_unseen_offline_updates_even_with_an_empty_queue(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    queue.discard_pending(now=int(NOW.timestamp()))
    restored = queue_at(tmp_path)
    for seconds in (-60, 0):
        old = forwarded(10 + seconds + 60)
        old.date = NOW + timedelta(seconds=seconds)
        assert not restored.enqueue(old)
    fresh = forwarded(100)
    fresh.date = NOW + timedelta(seconds=1)
    assert restored.enqueue(fresh)
    pending = restored.next_job()
    assert pending is not None and pending.first_id == 100


def test_discard_clears_ordering_stop_and_blocks_late_members_of_abandoned_album(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    for i in (1, 2):
        queue.enqueue(forwarded(i, kind="photo", group=7))
    queue.begin(queue.state.jobs[0])
    queue.enqueue(forwarded(3, kind="photo", group=7))
    assert queue.state.problem is not None
    queue.discard_pending(now=int(NOW.timestamp()))
    restored = queue_at(tmp_path)
    assert restored.state.problem is None
    late = forwarded(4, kind="photo", group=7)
    late.date = NOW + timedelta(seconds=1)
    assert not restored.enqueue(late)
    assert restored.next_job() is None


def test_combined_discard_and_cleanup_removes_all_task_files_and_is_repeatable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("tg_comment_uploader.reupload_maintenance.time.time", NOW.timestamp)
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded(2))
    complete = cache_files(queue, "message-1")
    abandoned = cache_files(queue, "message-2")
    unrelated = Path(queue.state.download_root) / "unrelated-file"
    unrelated.write_bytes(b"keep me")
    discard_pending(queue)
    delete_downloads(queue)
    assert not abandoned.parent.exists()
    assert not complete.parent.exists()
    assert unrelated.read_bytes() == b"keep me"
    restored = queue_at(tmp_path)
    discard_pending(restored)
    delete_downloads(restored)
    assert restored.next_job() is None


def test_checkpoint_failure_never_deletes_downloads(tmp_path: Path, monkeypatch) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    directory = cache_files(queue, "message-1")
    before = queue.path.read_bytes()

    def fail(*args, **kwargs):
        raise AppError("disk full")

    monkeypatch.setattr("tg_comment_uploader.reupload_queue.write_private_json", fail)
    with pytest.raises(AppError, match="disk full"):
        discard_pending(queue)
        delete_downloads(queue)
    assert queue.path.read_bytes() == before
    assert (directory / "media.bin").read_bytes() == b"completed bytes"
    assert queue.state.jobs[0].status == "queued"


def test_cleanup_failure_does_not_restore_abandoned_task(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    directory = cache_files(queue, "message-1")
    extra = directory / "user-file.txt"
    extra.write_bytes(b"do not delete")
    with pytest.raises(AppError, match="queue state retained"):
        discard_pending(queue)
        delete_downloads(queue)
    restored = queue_at(tmp_path)
    assert restored.next_job() is None
    assert extra.read_bytes() == b"do not delete"
    extra.unlink()
    discard_pending(restored)
    delete_downloads(restored)
    assert not directory.exists()


@pytest.mark.parametrize("level", ["namespace", "job", "message"])
def test_cleanup_refuses_symlink_directory_without_touching_external_data(
    tmp_path: Path, level: str
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    target = tmp_path / "external"
    target.mkdir()
    (target / "media.bin").write_bytes(b"precious")
    link = queue.download_directory()
    if level in {"job", "message"}:
        link /= "message-1"
    if level == "message":
        link /= "1"
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(AppError, match="queue state retained"):
        discard_pending(queue)
        delete_downloads(queue)
    assert (target / "media.bin").read_bytes() == b"precious"
    assert queue_at(tmp_path).next_job() is None


def configure_cli(tmp_path, monkeypatch, queue):
    (tmp_path / "justfile").touch()
    config = AppConfig.model_validate(
        {
            "bot": {"token": f"{BOT}:fake-token", "api_id": 123, "api_hash": "fake-hash"},
            "profiles": {},
            "reupload": {"chat_id": CHAT, "download_dir": queue.state.download_root},
        }
    )
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.load_config", lambda path: config)
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.upload_instance_lock",
        lambda: upload_instance_lock(tmp_path),
    )
    monkeypatch.setattr("tg_comment_uploader.reupload_cli.ReuploadQueue", lambda *a, **kw: queue)
    paths = SimpleNamespace(
        pending_path=tmp_path / "pending.json",
        session_path=tmp_path / "fake.session",
        owner=SimpleNamespace(config_fingerprint="fake", bot_id=BOT),
    )
    monkeypatch.setattr(
        "tg_comment_uploader.reupload_cli.get_mtproto_paths", lambda *a, **kw: paths
    )

    listeners = []

    class Listener:
        def __init__(self, **kwargs):
            self.service = kwargs["client_factory"].__self__

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def run_service(self, operation):
            operation.close()
            listeners.append(self.service)

    monkeypatch.setattr("tg_comment_uploader.reupload_cli.MtprotoSender", Listener)
    return listeners


def test_discard_runs_before_listening_is_locked_and_status_reports_terminal_counts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    queue = queue_at(tmp_path)
    original = forwarded(1, source_post=900)
    queue.enqueue(original)
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded_again(original, 2))
    queue.update(
        queue.get("message-2").model_copy(
            update={"status": "duplicate", "duplicate_of": "message-1"}
        )
    )
    queue.enqueue(forwarded(3))
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    before = queue.path.read_bytes()
    with upload_instance_lock(tmp_path):
        assert main(["reupload", "--discard-pending"]) == 1
    assert "already running" in capsys.readouterr().err
    assert queue.path.read_bytes() == before
    assert not listeners
    assert main(["reupload", "--discard-pending"]) == 0
    assert len(listeners) == 1 and listeners[0].queue.next_job() is None
    assert main(["reupload", "--status"]) == 0
    output = capsys.readouterr().out
    assert "1 confirmed, 0 pending, 0 legacy skipped, 2 abandoned" in output
    assert "fake-token" not in output and "fake-hash" not in output


def test_delete_flag_cleans_finished_files_and_preserves_pending_downloads(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded(2))
    complete = cache_files(queue, "message-1")
    pending = cache_files(queue, "message-2")
    before = queue.path.read_bytes()
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    assert main(["reupload", "--delete-downloads"]) == 0
    assert "1 unfinished task(s) retained" in capsys.readouterr().out
    assert not complete.parent.exists()
    assert (pending / "media.bin").read_bytes() == b"completed bytes"
    assert queue.path.read_bytes() == before
    assert len(listeners) == 1


@pytest.mark.parametrize("flag", ["--discard-pending", "--delete-downloads"])
def test_status_refuses_mutating_flags(tmp_path: Path, monkeypatch, capsys, flag: str) -> None:
    listeners = configure_cli(tmp_path, monkeypatch, queue_at(tmp_path))
    assert main(["reupload", "--status", flag]) == 1
    assert "--status cannot be combined" in capsys.readouterr().err
    assert not listeners


@pytest.mark.parametrize("keep", [False, True])
def test_cli_deletes_after_upload_by_default_and_keep_flag_overrides_it(
    tmp_path: Path, monkeypatch, keep: bool
) -> None:
    listeners = configure_cli(tmp_path, monkeypatch, queue_at(tmp_path))
    assert main(["reupload", *(["--keep-downloads"] if keep else [])]) == 0
    assert len(listeners) == 1 and listeners[0].keep_downloads is keep


def test_uncertain_sending_task_gets_an_explicit_delivery_notice(tmp_path: Path, capsys) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.sending("message-1")
    discard_pending(queue)
    assert "1 abandoned task(s) had uncertain delivery" in capsys.readouterr().out
    assert queue_at(tmp_path).get("message-1").abandoned_from == "sending"
