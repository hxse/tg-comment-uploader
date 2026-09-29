"""Connect the existing Bot identity and protect its persistent session."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telethon import utils

from .errors import NonRetryableUploadError, TelegramUploadError
from .mtproto_errors import _translate_exception
from .mtproto_runtime import _disconnect_client, _observe_disconnected_future

if TYPE_CHECKING:
    from .mtproto_sender import MtprotoSender


async def _ensure_connected(self: MtprotoSender) -> tuple[Any, Any]:
    if self._client is not None and self._peer is not None:
        return self._client, self._peer

    try:
        client = self._client_factory(
            str(self._session_path),
            self._api_id,
            self._api_hash,
            request_retries=0,
            connection_retries=0,
            retry_delay=0,
            auto_reconnect=False,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
        )
        self._client = client
        _protect_session_files(client)
        try:
            await client.start(bot_token=self._bot_token)
        finally:
            # Telethon reports a transport failure both through the active
            # request and through client.disconnected. The request error is
            # handled by this sender, so also observe the lifecycle future
            # to prevent the same exception being logged much later as an
            # unretrieved Future during event-loop cleanup.
            _observe_disconnected_future(client)
        me = await client.get_me()
        if me is None or getattr(me, "bot", None) is not True:
            raise NonRetryableUploadError(
                "the persistent Telegram session is not authenticated as a bot"
            )
        actual_bot_id = getattr(me, "id", None)
        if (
            isinstance(actual_bot_id, bool)
            or not isinstance(actual_bot_id, int)
            or actual_bot_id != self._expected_bot_id
        ):
            raise NonRetryableUploadError(
                "the persistent Telegram session belongs to a different bot token"
            )
        _protect_session_files(client)
        peer = await _resolve_peer(self, client)
        _protect_session_files(client)
        peer_id = utils.get_peer_id(peer)
        if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
            raise NonRetryableUploadError("Telegram resolved chat_id to an invalid peer")
        if self._peer_resolved is not None:
            self._peer_resolved(peer_id)
        self._peer = peer
        return client, peer
    except BaseException as exc:
        await _discard_failed_client(self)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        if isinstance(exc, TelegramUploadError):
            raise
        raise _translate_exception(
            exc,
            final_request_started=False,
            secrets=self._secrets,
            context="connecting to Telegram",
        ) from exc


async def _resolve_peer(self, client: Any) -> Any:
    try:
        if isinstance(self._chat_id, str):
            # Unlike get_input_entity(str), get_entity(str) performs a fresh
            # public API lookup, so a renamed username cannot silently reuse a
            # stale peer from the persistent session cache.
            return utils.get_input_peer(await client.get_entity(self._chat_id))
        return await client.get_input_entity(self._chat_id)
    except (TypeError, ValueError) as exc:
        raise NonRetryableUploadError(
            f"Telegram could not resolve chat_id {self._chat_id!r}; make sure the bot "
            "can access that channel or group"
        ) from exc


async def _discard_failed_client(self: MtprotoSender) -> None:
    client = self._client
    self._client = None
    self._peer = None
    if client is None:
        return
    try:
        await _disconnect_client(client)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException:
        pass
    try:
        _protect_session_files(client)
    except TelegramUploadError:
        pass


def _prepare_private_session_directory(session_path: Path) -> None:
    try:
        session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_status = session_path.parent.lstat()
        if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
            raise OSError("session parent is not a real directory")
        if os.name == "posix":
            session_path.parent.chmod(0o700)
    except OSError as exc:
        raise NonRetryableUploadError(
            f"failed to prepare private Telegram session directory {session_path.parent}: {exc}"
        ) from exc


def _protect_session_files(client: Any) -> None:
    if os.name != "posix":
        return
    filename = getattr(getattr(client, "session", None), "filename", None)
    if not filename:
        return
    _protect_session_path_candidates(Path(filename))


def _protect_session_path_candidates(session_path: Path) -> None:
    if os.name != "posix":
        return
    candidates = (
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
    )
    for path in candidates:
        try:
            path_status = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to inspect persistent Telegram session file {path}: {exc}"
            ) from exc
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise NonRetryableUploadError(
                f"persistent Telegram session path is not a regular file: {path}"
            )
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise NonRetryableUploadError(
                f"failed to protect persistent Telegram session file {path}: {exc}"
            ) from exc
