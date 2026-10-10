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
    UploadResponse,
    WhoamiResponse,
)
from .api.types import EventId, EventType, RoomIdentifier, TxnId, UserId
from .config import BotInfo
from .crypto.attachments import encrypt_attachment
from .crypto.primitives import MEGOLM_ALGORITHM
from .crypto.types import CryptoError
from .event import Event, MessageEvent
from .message import build_message_content
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
        uploader = _RoomMediaUploader(self, encrypted=bool(settings))
        content = await build_message_content(message, bot=uploader)
        return await self.send_event(
            room_id=room_id, event_type="m.room.message", txn_id=txn_id, content=content
        )

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
