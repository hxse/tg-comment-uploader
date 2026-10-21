from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reupload_fakes import BOT, FakeBot, forwarded, queue_at, sender_at
from test_reupload_completion import CompletionProgress
from test_reupload_delivery import process_all
from test_reupload_discard import configure_cli
from tg_comment_uploader.cli import build_parser, main
from tg_comment_uploader.errors import AppError
from tg_comment_uploader.reupload_download import job_directory
from tg_comment_uploader.reupload_service import ReuploadService


@pytest.mark.parametrize("keep_after_upload", [False, True])
def test_download_retains_text_media_and_albums_then_reupload_reuses_local_files(
    tmp_path: Path, keep_after_upload: bool
) -> None:
    messages = [
        forwarded(1, text="本地文字 😀"),
        forwarded(2, kind="photo", group=7),
        forwarded(3, kind="document", group=7),
        forwarded(4, kind="document"),
    ]
    queue = queue_at(tmp_path)
    for message in reversed(messages):
        queue.enqueue(message)
    original_ids = [job.random_ids for job in queue.state.jobs]
    bot = FakeBot(messages)
    process_all(tmp_path, bot, queue, keep=False, download_only=True)

    assert not bot.requests and not bot.parts and not bot.delivered
    assert bot.timeline == [("download", 2), ("download", 3), ("download", 4)]
    assert all(job.status == "ready" and not job.output_ids for job in queue.state.jobs)
    saved_files = {}
    for job in queue.state.jobs:
        for item in job.messages:
            directory = job_directory(queue, job) / str(item.message_id)
            metadata = json.loads((directory / "message.json").read_text())
            assert metadata["text"] == messages[item.message_id - 1].message
            if item.message_id != 1:
                name = "media.jpg" if item.message_id == 2 else "media.bin"
                assert (directory / name).read_bytes() == bot.payload
            if item.message_id in (3, 4):
                assert (directory / "thumbnail.jpg").is_file()
            saved_files.update({path: path.read_bytes() for path in directory.iterdir()})

    # A download restart skips completed work without changing send identities.
    before = queue.path.read_bytes()
    restarted = FakeBot([])
    process_all(tmp_path, restarted, queue_at(tmp_path), download_only=True)
    assert not restarted.timeline and not restarted.requests
    assert queue.path.read_bytes() == before
    assert all(path.read_bytes() == content for path, content in saved_files.items())

    # Switching to reupload sends the retained snapshot even after source deletion.
    uploader = FakeBot([])
    restored = queue_at(tmp_path)
    process_all(tmp_path, uploader, restored, keep=keep_after_upload)
    assert uploader.sent_payloads == [bot.payload] * 3
    assert uploader.sent_text == [message.message for message in messages]
    assert not uploader.download_offsets and not uploader.thumbnail_downloads
    assert [job.random_ids for job in restored.state.jobs] == original_ids
    assert all(job.status == "confirmed" for job in restored.state.jobs)
    assert all(path.exists() == keep_after_upload for path in saved_files)


def test_interrupted_download_resumes_without_uploading_or_losing_later_jobs(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1, kind="document"))
    queue.enqueue(forwarded(2))
    ids = [job.random_ids for job in queue.state.jobs]
    bot = FakeBot()
    bot.fail_download = 1
    with pytest.raises(AppError, match="rerun just download"):
        process_all(tmp_path, bot, queue, download_only=True)
    first = queue.state.jobs[0]
    directory = job_directory(queue, first) / "1"
    assert (directory / "media.bin.part").read_bytes() == bot.payload[:4]
    assert (directory / "media.bin.part.json").exists()
    assert first.status == "downloading"
    assert queue.state.jobs[1].status == "queued"
    assert not bot.requests

    resumed = FakeBot()
    restored = queue_at(tmp_path)
    process_all(tmp_path, resumed, restored, download_only=True)
    assert (directory / "media.bin").read_bytes() == bot.payload
    assert not (directory / "media.bin.part").exists()
    assert all(job.status == "ready" for job in restored.state.jobs)
    assert [job.random_ids for job in restored.state.jobs] == ids
    assert not resumed.requests and not resumed.parts


def test_download_preserves_uncertain_upload_and_continues_later_downloads(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.sending("message-1")
    uncertain = queue.get("message-1")
    queue.enqueue(forwarded(2, kind="document"))
    bot = FakeBot()
    process_all(tmp_path, bot, queue, download_only=True)

    assert queue.get("message-1") == uncertain
    assert queue.get("message-2").status == "ready"
    assert queue.next_job(download_only=True) is None
    assert queue.next_job() == uncertain
    assert not bot.requests and not bot.delivered


def test_download_listener_reports_completion_once_and_accepts_the_next_batch(
    tmp_path: Path,
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.confirm("message-1", (100,))
    queue.enqueue(forwarded(2, kind="photo", group=7))
    queue.enqueue(forwarded(3, kind="document", group=7))
    progress = CompletionProgress()
    service = ReuploadService(
        queue, bot_id=BOT, retries=0, download_only=True, settle_seconds=0, progress=progress
    )
    bot = FakeBot()
    with sender_at(tmp_path, bot) as sender:

        async def scenario() -> None:
            listener = asyncio.create_task(service.run(sender))
            try:
                await asyncio.wait_for(progress.completed.wait(), 3)
                assert "1 task(s), 2 message(s) downloaded" in progress.summaries[0]
                await asyncio.sleep(0.3)
                assert len(progress.summaries) == 1
                progress.completed.clear()
                await service.on_message(SimpleNamespace(message=forwarded(4)))
                await asyncio.wait_for(progress.completed.wait(), 3)
                assert len(progress.summaries) == 2
                assert "1 task(s), 1 message(s) downloaded" in progress.summaries[1]
            finally:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)

        sender.run_service(scenario())
    assert queue.get("message-1").status == "confirmed"
    assert queue.get("album-7").status == queue.get("message-4").status == "ready"
    assert not bot.requests and not bot.delivered


@pytest.mark.parametrize("configured_keep", [False, True])
@pytest.mark.parametrize(
    ("arguments", "keep", "download_only"),
    [
        (["reupload", "--no-keep-downloads"], False, False),
        (["reupload", "--keep-downloads"], True, False),
        (["download"], True, True),
        (["reupload"], None, False),
    ],
)
def test_cli_modes_override_config_and_preserve_legacy_config_default(
    tmp_path: Path, monkeypatch, arguments, keep, download_only, configured_keep: bool
) -> None:
    from tg_comment_uploader import reupload_cli

    listeners = configure_cli(tmp_path, monkeypatch, queue_at(tmp_path))
    config = reupload_cli.load_config(Path("unused"))
    assert config.reupload is not None
    configured = config.model_copy(
        update={"reupload": config.reupload.model_copy(update={"keep_downloads": configured_keep})}
    )
    monkeypatch.setattr(reupload_cli, "load_config", lambda path: configured)
    assert main(arguments) == 0
    assert len(listeners) == 1
    assert listeners[0].keep_downloads is (configured_keep if keep is None else keep)
    assert listeners[0].download_only is download_only


def test_download_status_is_offline_and_distinguishes_saved_and_uncertain_jobs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    queue = queue_at(tmp_path)
    queue.enqueue(forwarded(1))
    queue.sending("message-1")
    queue.enqueue(forwarded(2))
    queue.ready("message-2")
    queue.enqueue(forwarded(3))
    before = queue.path.read_bytes()
    listeners = configure_cli(tmp_path, monkeypatch, queue)
    assert main(["download", "--status"]) == 0
    assert (
        "1 downloaded, 1 pending download, 1 awaiting upload confirmation"
        in capsys.readouterr().out
    )
    assert not listeners
    assert queue.path.read_bytes() == before


@pytest.mark.parametrize("flag", ["--delete-downloads", "--no-keep-downloads"])
def test_download_command_rejects_cleanup_flags(flag: str) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["download", flag])
    assert caught.value.code == 2
