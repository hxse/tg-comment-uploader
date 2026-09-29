"""Build fresh media/text requests and validate final confirmations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from telethon import functions, types, utils

from .errors import (
    NonRetryableUploadError,
    RetryableUploadError,
    TelegramUploadError,
    sanitize_untrusted_error_text,
)
from .mtproto_errors import _translate_exception
from .mtproto_files import _upload_media
from .mtproto_protocol import (
    _ResponseValidationError,
    _validate_final_request_serializable,
    parse_send_result,
)
from .telegram_sender import (
    BeforeFinalRequest,
    MediaGroupUploadProgress,
    UploadItem,
    UploadProgress,
)

if TYPE_CHECKING:
    from .mtproto_sender import MtprotoSender


async def _send_media(
    self: MtprotoSender,
    item: UploadItem,
    *,
    random_id: int,
    progress_callback: UploadProgress | None,
    before_final_request: BeforeFinalRequest | None,
) -> int:
    client, peer = await self._ensure_connected()
    self._raise_if_operation_aborted()
    media = await _upload_media(
        self,
        client,
        item,
        progress_callback=progress_callback,
    )
    self._raise_if_operation_aborted()
    request = functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=item.caption,
        entities=await _input_entities(client, item.entities),
        invert_media=item.invert_media,
        reply_to=self._reply_to,
        random_id=random_id,
    )
    result = await _invoke_final_request(
        self,
        client,
        request,
        before_final_request=before_final_request,
    )
    try:
        return parse_send_result(
            result,
            (random_id,),
            expected_peer_id=utils.get_peer_id(peer),
            expect_grouped=False,
        )[0]
    except _ResponseValidationError as exc:
        raise RetryableUploadError(
            f"Telegram returned an incomplete send result: {exc}",
            outcome_uncertain=True,
            final_request_started=True,
        ) from exc


async def _send_media_group(
    self: MtprotoSender,
    items: tuple[UploadItem, ...],
    *,
    random_ids: tuple[int, ...],
    progress_callback: MediaGroupUploadProgress | None,
    before_final_request: BeforeFinalRequest | None,
) -> tuple[int, ...]:
    client, peer = await self._ensure_connected()
    self._raise_if_operation_aborted()
    multi_media: list[types.InputSingleMedia] = []

    for item_index, (item, random_id) in enumerate(zip(items, random_ids, strict=True)):
        self._raise_if_operation_aborted()
        per_file_progress: UploadProgress | None = None
        if progress_callback is not None:

            def report_file_progress(
                sent: int,
                total: int,
                *,
                index: int = item_index,
            ) -> None:
                progress_callback(index, sent, total)

            per_file_progress = report_file_progress
        uploaded_media = await _upload_media(
            self,
            client,
            item,
            progress_callback=per_file_progress,
        )
        self._raise_if_operation_aborted()
        try:
            materialized = await client(
                functions.messages.UploadMediaRequest(
                    peer=peer,
                    media=uploaded_media,
                )
            )
            self._raise_if_operation_aborted()
            if item.kind == "photo":
                photo = getattr(materialized, "photo", None)
                if not isinstance(photo, types.Photo):
                    raise NonRetryableUploadError("Telegram did not materialize the uploaded photo")
                reusable_media = types.InputMediaPhoto(
                    id=utils.get_input_photo(photo), spoiler=item.spoiler
                )
            else:
                document = getattr(materialized, "document", None)
                if not isinstance(document, types.Document):
                    raise NonRetryableUploadError(
                        "Telegram did not materialize the uploaded document"
                    )
                reusable_media = types.InputMediaDocument(
                    id=utils.get_input_document(document), spoiler=item.spoiler
                )
        except TelegramUploadError:
            raise
        except Exception as exc:
            raise _translate_exception(
                exc,
                final_request_started=False,
                secrets=self._secrets,
                context=f"preparing media-group item {item_index + 1}",
            ) from exc

        multi_media.append(
            types.InputSingleMedia(
                media=reusable_media,
                message=item.caption,
                entities=await _input_entities(client, item.entities),
                random_id=random_id,
            )
        )

    request = functions.messages.SendMultiMediaRequest(
        peer=peer,
        multi_media=multi_media,
        reply_to=self._reply_to,
        invert_media=items[0].invert_media,
    )
    result = await _invoke_final_request(
        self,
        client,
        request,
        before_final_request=before_final_request,
    )
    try:
        return parse_send_result(
            result,
            random_ids,
            expected_peer_id=utils.get_peer_id(peer),
            expect_grouped=True,
        )
    except _ResponseValidationError as exc:
        raise RetryableUploadError(
            f"Telegram returned an incomplete media-group result: {exc}",
            outcome_uncertain=True,
            final_request_started=True,
        ) from exc


async def _invoke_final_request(
    self: MtprotoSender,
    client: Any,
    request: Any,
    *,
    before_final_request: BeforeFinalRequest | None,
) -> Any:
    self._raise_if_operation_aborted()
    _validate_final_request_serializable(request)
    self._raise_if_operation_aborted()
    if before_final_request is not None:
        try:
            before_final_request()
        except TelegramUploadError:
            raise
        except Exception as exc:
            raise NonRetryableUploadError(
                "failed to persist upload state before the final Telegram request: "
                f"{sanitize_untrusted_error_text(str(exc), secrets=self._secrets)}"
            ) from exc
    self._raise_if_operation_aborted()
    _notify_final_request_status(self, True)
    self._final_request_started_for_operation = True
    try:
        try:
            return await asyncio.wait_for(
                client(request),
                timeout=self._final_request_timeout_seconds,
            )
        except Exception as exc:
            raise _translate_exception(
                exc,
                final_request_started=True,
                secrets=self._secrets,
                context="sending the final Telegram request",
            ) from exc
    finally:
        _notify_final_request_status(self, False)


def _notify_final_request_status(self: MtprotoSender, active: bool) -> None:
    callback = self._final_request_status
    if callback is None:
        return
    try:
        callback(active)
    except Exception:
        # A terminal renderer must never alter Telegram delivery semantics.
        pass


async def _input_entities(client: Any, entities: tuple[Any, ...]) -> list[Any] | None:
    converted = []
    for entity in entities:
        if isinstance(entity, types.MessageEntityMentionName):
            user = utils.get_input_user(await client.get_input_entity(entity.user_id))
            entity = types.InputMessageEntityMentionName(entity.offset, entity.length, user)
        converted.append(entity)
    return converted or None


async def send_text(
    sender: MtprotoSender,
    text: str,
    *,
    entities: tuple[Any, ...],
    random_id: int,
    before_final_request: BeforeFinalRequest,
) -> tuple[int, ...]:
    client, peer = await sender._ensure_connected()
    request = functions.messages.SendMessageRequest(
        peer=peer,
        message=text,
        random_id=random_id,
        entities=await _input_entities(client, entities),
        no_webpage=True,
    )
    result = await _invoke_final_request(
        sender, client, request, before_final_request=before_final_request
    )
    try:
        return parse_send_result(
            result, (random_id,), expected_peer_id=utils.get_peer_id(peer), expect_grouped=False
        )
    except _ResponseValidationError as exc:
        raise RetryableUploadError(
            "Telegram returned an incomplete text result",
            outcome_uncertain=True,
            final_request_started=True,
        ) from exc
