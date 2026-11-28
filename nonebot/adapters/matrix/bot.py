from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any
from typing_extensions import override
from uuid import uuid4

from nonebot.adapters import (
    Bot as BaseBot,
    Event as BaseEvent,
    Message as BaseMessage,
    MessageSegment as BaseMessageSegment,
)

from nonebot.message import handle_event

from .api import (
    ApiClient,
    EventIdResponse,
    JoinRoomResponse,
    RawMatrixEvent,
    UploadResponse,
    WhoamiResponse,
)
from .api.types import EventId, EventType, RoomIdentifier, TxnId, UserId
from .config import BotInfo
from .crypto.attachments import encrypt_attachment
from .crypto.primitives import MEGOLM_ALGORITHM
from .crypto.types import CryptoError
from .event import Event, MessageEvent
from .message import (
    MATRIX_HTML_FORMAT,
    add_reply_fallback,
    build_edit_content,
    build_message_content,
    get_reply_event_id,
)
from .room import Room

if TYPE_CHECKING:
    from .adapter import Adapter
    from .crypto import CryptoEngine


def make_txn_id() -> str:
    return uuid4().hex


class Bot(BaseBot, ApiClient):
    _adapter: Adapter

    @override
    def __init__(
        self,
        adapter: Adapter,
        self_id: str,
        bot_info: BotInfo,
        self_info: WhoamiResponse,
    ) -> None:
        super().__init__(adapter, self_id)
        self.adapter = self._adapter = adapter
        self._bot_info = bot_info
        self._self_info = self_info
        self.next_batch: str | None = None
        self.startup_time_ms = int(time() * 1000)
        self.direct_rooms: set[str] = set()
        self.crypto: CryptoEngine | None = None
        self._rooms: dict[str, str] = {}
        self._encryption_states: dict[str, dict[str, Any] | None] = {}
        self._history_visibility: dict[str, str] = {}
        self._dispatched_events: set[str] = set()
        # E2EE crypto engine, initialized by Adapter.run_bot()

    @override
    def __repr__(self) -> str:
        return f"Bot(type={self.type!r}, self_id={self.self_id!r})"

    @property
    def bot_info(self) -> BotInfo:
        return self._bot_info

    @property
    def self_info(self) -> WhoamiResponse:
        return self._self_info

    def update_self_info(self, self_info: WhoamiResponse) -> None:
        """Update the internal self_info after token refresh."""
        self._self_info = self_info

    @property
    def user_id(self) -> UserId:
        return self._self_info.user_id

    @property
    def device_id(self) -> str | None:
        return self._self_info.device_id

    async def handle_event(self, event: Event) -> None:
        await handle_event(self, event)

    @override
    async def send(
        self,
        event: BaseEvent,
        message: str | BaseMessage | BaseMessageSegment,
        **kwargs: Any,
    ) -> EventIdResponse:
        if not isinstance(event, MessageEvent) or event.room_id is None:
            msg = "Matrix messages can only be sent to events with a room_id"
            raise ValueError(msg)
        return await self.send_to(event.room_id, message, **kwargs)

    async def send_to(
        self,
        room_id: RoomIdentifier,
        message: str | BaseMessage | BaseMessageSegment,
        **kwargs: Any,  # noqa: ANN401
    ) -> EventIdResponse:
        txn_id = kwargs.pop("txn_id", None) or make_txn_id()
        settings = await self._room_encryption_settings(str(room_id))
        if settings and (self.crypto is None or not self.crypto.ready):
            msg = "Encryption is unavailable for this room"
            raise CryptoError(msg)
        if settings and settings.get("algorithm") != MEGOLM_ALGORITHM:
            msg = "Unsupported room encryption algorithm"
            raise CryptoError(msg)
        reply_event_id = get_reply_event_id(message)
        target = None
        if reply_event_id is not None:
            target = await self.get_room_event(room_id=room_id, event_id=reply_event_id)
            if target.type == "m.room.encrypted":
                if self.crypto is None:
                    msg = "Cannot build reply fallback for an encrypted event"
                    raise CryptoError(msg)
                target = await self.crypto.decrypt_room_event(
                    target, room_id=str(room_id)
                )
                if target is None:
                    msg = "Cannot decrypt the reply target event"
                    raise CryptoError(msg)
        uploader = _RoomMediaUploader(self, encrypted=bool(settings))
        content = await build_message_content(message, bot=uploader)
        if target is not None:
            add_reply_fallback(
                content, target.model_dump(mode="json"), room_id=str(room_id)
            )
        return await self.send_event(
            room_id=room_id, event_type="m.room.message", txn_id=txn_id, content=content
        )

    async def edit_message(
        self,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        message: str | BaseMessage | BaseMessageSegment,
        *,
        txn_id: str | None = None,
    ) -> EventIdResponse:
        """Edit a message sent by this bot."""
        target = await self._get_edit_target(room_id, event_id)
        content = build_edit_content(message, str(event_id))
        new_content = content["m.new_content"]
        mentions = new_content.get("m.mentions")
        if isinstance(mentions, dict):
            previous_mentions = target.content.get("m.mentions")
            previous_mentions = (
                previous_mentions if isinstance(previous_mentions, dict) else {}
            )
            previous_user_ids = previous_mentions.get("user_ids", [])
            if not isinstance(previous_user_ids, list):
                previous_user_ids = []
            previous_user_ids = {
                user_id for user_id in previous_user_ids if isinstance(user_id, str)
            }
            new_mentions: dict[str, Any] = {}
            user_ids = mentions.get("user_ids", [])
            if not isinstance(user_ids, list):
                user_ids = []
            new_user_ids = sorted(
                {user_id for user_id in user_ids if isinstance(user_id, str)}
                - previous_user_ids
            )
            if new_user_ids:
                new_mentions["user_ids"] = new_user_ids
            if mentions.get("room") and not previous_mentions.get("room"):
                new_mentions["room"] = True
            if new_mentions:
                content["m.mentions"] = new_mentions
        return await self.send_event(
            room_id=room_id,
            event_type="m.room.message",
            txn_id=txn_id or make_txn_id(),
            content=content,
        )

    async def edit_media_caption(
        self,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        caption: str | None,
        *,
        formatted_caption: str | None = None,
        txn_id: str | None = None,
    ) -> EventIdResponse:
        """Edit or remove the caption of a media message sent by this bot."""
        target = await self._get_edit_target(room_id, event_id)
        content = target.content
        if content.get("msgtype") not in {"m.image", "m.file", "m.audio", "m.video"}:
            raise ValueError("Target event is not a supported media message")  # noqa: TRY003, EM101

        replacement = dict(content)
        replacement.pop("m.relates_to", None)
        replacement.pop("m.new_content", None)
        filename = replacement.get("filename")
        voice_file = replacement.get("org.matrix.msc1767.file")
        is_voice = isinstance(voice_file, dict)
        if not isinstance(filename, str):
            filename = (
                voice_file.get("name") if is_voice else replacement.get("body", "")
            )
        if not isinstance(filename, str):
            filename = replacement.get("body", "")
        if caption is None:
            replacement["body"] = filename
            replacement.pop("filename", None)
        else:
            replacement["body"] = caption
            if not is_voice:
                replacement["filename"] = filename
        if formatted_caption is None:
            replacement.pop("format", None)
            replacement.pop("formatted_body", None)
        else:
            replacement["format"] = MATRIX_HTML_FORMAT
            replacement["formatted_body"] = formatted_caption
        if is_voice:
            replacement["org.matrix.msc1767.text"] = replacement["body"]

        fallback_body = caption if caption is not None else replacement["body"]
        edit_content = {
            **replacement,
            "body": f"* {fallback_body}",
            "m.new_content": replacement,
            "m.relates_to": {"rel_type": "m.replace", "event_id": str(event_id)},
        }
        # Keep existing mentions in m.new_content without notifying again.
        edit_content.pop("m.mentions", None)
        return await self.send_event(
            room_id=room_id,
            event_type="m.room.message",
            txn_id=txn_id or make_txn_id(),
            content=edit_content,
        )

    async def _get_edit_target(
        self, room_id: RoomIdentifier, event_id: EventId | str
    ) -> RawMatrixEvent:
        target = await self.get_room_event(room_id=room_id, event_id=event_id)
        if target.type == "m.room.encrypted":
            if self.crypto is None:
                raise CryptoError("Cannot edit an encrypted event without E2EE")  # noqa: TRY003, EM101
            target = await self.crypto.decrypt_room_event(target, room_id=str(room_id))
            if target is None:
                raise CryptoError("Cannot decrypt the edit target event")  # noqa: TRY003, EM101
        if target.type != "m.room.message":
            raise ValueError("Only m.room.message events can be edited")  # noqa: TRY003, EM101
        if target.sender != self.user_id:
            raise ValueError("Only messages sent by this bot can be edited")  # noqa: TRY003, EM101
        relation = target.content.get("m.relates_to")
        if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
            raise ValueError("An edit event cannot be edited")  # noqa: TRY003, EM101
        return target

    def encryption(self) -> CryptoEngine:
        if self.crypto is None or not self.crypto.ready:
            msg = "E2EE is disabled or has not initialized"
            raise CryptoError(msg)
        return self.crypto

    def get_room(self, room_id: str) -> Room | None:
        return Room(self, room_id) if room_id in self._rooms else None

    async def _room_encryption_settings(self, room_id: str) -> dict[str, Any] | None:
        if room_id not in self._encryption_states:
            # A failed state query cannot establish that plaintext is safe.
            state = await self.get_room_state(room_id=room_id)
            settings = None
            for raw in state.events:
                if raw.type == "m.room.encryption":
                    settings = raw.content
                elif raw.type == "m.room.history_visibility":
                    self._history_visibility[room_id] = raw.content.get(
                        "history_visibility", "shared"
                    )
            self._encryption_states[room_id] = settings
        value = self._encryption_states[room_id]
        if value:
            return {
                **value,
                "history_visibility": self._history_visibility.get(room_id, "shared"),
            }
        return None

    @override
    async def send_message(
        self, *, room_id: RoomIdentifier, txn_id: TxnId, content: dict[str, Any]
    ) -> EventIdResponse:
        return await self.send_event(
            room_id=room_id, event_type="m.room.message", txn_id=txn_id, content=content
        )

    @override
    async def send_event(
        self,
        *,
        room_id: RoomIdentifier,
        event_type: EventType,
        txn_id: TxnId,
        content: dict[str, Any],
    ) -> EventIdResponse:
        """Send an arbitrary room event via _api_send_event.

        Supports custom event types such as m.room.encrypted.
        """
        room = str(room_id)
        if room in self._rooms and self._rooms[room] != "join":
            msg = "Sending requires a joined room"
            raise CryptoError(msg)
        if self.crypto is not None:
            return await self.crypto.send_room_event(
                room, str(event_type), content, str(txn_id)
            )
        settings = await self._room_encryption_settings(room)
        if settings and event_type != "m.room.encrypted":
            msg = "Encryption is unavailable for this room"
            raise CryptoError(msg)
        return await self.call_api(
            "send_event",
            room_id=room_id,
            event_type=event_type,
            txn_id=txn_id,
            content=content,
        )

    async def upload_media(
        self,
        content: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadResponse:
        return await self.upload_content(
            content=content,
            filename=filename,
            content_type=content_type,
        )

    async def react(
        self,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        key: str,
        *,
        txn_id: str | None = None,
    ) -> EventIdResponse:
        room = str(room_id)
        if await self._room_encryption_settings(room):
            return await self.send_event(
                room_id=room_id,
                event_type="m.reaction",
                txn_id=txn_id or make_txn_id(),
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": str(event_id),
                        "key": key,
                    }
                },
            )
        return await self.create_reaction(
            room_id=room_id,
            event_id=event_id,
            key=key,
            txn_id=txn_id or make_txn_id(),
        )

    async def redact(
        self,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        *,
        reason: str | None = None,
        txn_id: str | None = None,
    ) -> EventIdResponse:
        return await self.redact_event(
            room_id=room_id,
            event_id=event_id,
            txn_id=txn_id or make_txn_id(),
            reason=reason,
        )

    @override
    async def join_room(
        self,
        *,
        room_id: RoomIdentifier,
        reason: str | None = None,
    ) -> JoinRoomResponse:
        """Join a Matrix room by ID. Returns the room_id of the joined room."""
        return await self.call_api(  # type: ignore[return-value]
            "join_room", room_id=room_id, reason=reason
        )

    async def set_typing_state(
        self,
        room_id: RoomIdentifier,
        *,
        typing: bool = True,
        timeout: int | None = None,
    ) -> None:
        await self.set_typing(
            room_id=room_id,
            user_id=self.user_id,
            typing=typing,
            timeout=timeout,
        )

    async def mark_read(
        self,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        *,
        receipt_type: str = "m.read",
        thread_id: str | None = None,
    ) -> None:
        await self.post_receipt(
            room_id=room_id,
            receipt_type=receipt_type,
            event_id=event_id,
            thread_id=thread_id,
        )


class _RoomMediaUploader:
    def __init__(self, bot: Bot, *, encrypted: bool) -> None:
        self.bot, self.encrypted = bot, encrypted

    async def upload_media(
        self,
        content: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadResponse:
        return await self.bot.upload_media(
            content, filename=filename, content_type=content_type
        )

    async def upload_encrypted_media(self, content: bytes) -> dict[str, Any]:
        ciphertext, descriptor = encrypt_attachment(content)
        uploaded = await self.bot.upload_media(
            ciphertext, content_type="application/octet-stream"
        )
        return {**descriptor, "url": str(uploaded.content_uri)}
