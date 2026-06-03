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
from .api.types import EventId, RoomIdentifier, UserId
from .config import BotInfo
from .event import Event, MessageEvent
from .message import build_message_content

if TYPE_CHECKING:
    from .adapter import Adapter


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
        **kwargs: Any,
    ) -> EventIdResponse:
        kwargs.update(
            {
                "txn_id": kwargs.get("txn_id") or make_txn_id(),
                "content": await build_message_content(message, bot=self),
            }
        )
        return await self.send_message(
            room_id=room_id,
            **kwargs,
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

    async def join_room(
        self,
        room_id: RoomIdentifier,
        *,
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
