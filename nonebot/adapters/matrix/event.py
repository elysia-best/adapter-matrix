from __future__ import annotations

from typing import Any, Literal
from typing_extensions import override

from nonebot.adapters import Event as BaseEvent

from pydantic import ConfigDict, Field, PrivateAttr

from .api.model import RawMatrixEvent
from .api.types import EventId, RoomId, UserId
from .message import Message, message_from_content

EventType = Literal["message", "notice", "meta"]


class Event(BaseEvent):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    type: str
    content: dict[str, Any] = Field(default_factory=dict)
    event_id: EventId | None = None
    sender: UserId | None = None
    room_id: RoomId | None = None
    origin_server_ts: int | None = None
    state_key: str | None = None
    unsigned: dict[str, Any] | None = None
    to_me: bool = False

    @override
    def get_type(self) -> str:
        return "notice"

    @override
    def get_event_name(self) -> str:
        return self.type

    @override
    def get_event_description(self) -> str:
        room = f" room={self.room_id}" if self.room_id else ""
        sender = f" sender={self.sender}" if self.sender else ""
        return f"{self.type}{room}{sender}"

    @override
    def get_message(self) -> Message:
        return Message()

    @override
    def get_user_id(self) -> str:
        return str(self.sender or "")

    @override
    def get_session_id(self) -> str:
        return str(self.room_id or self.sender or "")

    @override
    def is_tome(self) -> bool:
        return self.to_me


class MetaEvent(Event):
    @override
    def get_type(self) -> str:
        return "meta"


class NoticeEvent(Event):
    @override
    def get_type(self) -> str:
        return "notice"


class MessageEvent(Event):
    reply: EventId | None = None
    _message: Message | None = PrivateAttr(default=None)

    @override
    def get_type(self) -> str:
        return "message"

    @override
    def get_message(self) -> Message:
        if self._message is None:
            self._message = message_from_content(self.content)
        return self._message

    @override
    def get_event_description(self) -> str:
        return (
            f"Message {self.event_id or ''} from {self.sender or ''} "
            f"in {self.room_id or ''}: {self.get_message()}"
        )


class RoomMessageEvent(MessageEvent):
    pass


class EncryptedRoomEvent(MessageEvent):
    """m.room.encrypted 事件，表示一条 Megolm 加密的房间消息。

    解密成功后，content 会被替换为明文内容 (如同 m.room.message)，
    原始加密内容保留在 encrypted_content 中。
    """

    encrypted_content: dict[str, Any] = Field(default_factory=dict)


class RoomMemberEvent(NoticeEvent):
    @property
    def membership(self) -> str | None:
        value = self.content.get("membership")
        return value if isinstance(value, str) else None

    @override
    def get_event_description(self) -> str:
        return (
            f"Member {self.state_key or self.sender or ''} "
            f"{self.membership or 'updated'} in {self.room_id or ''}"
        )


class ReactionEvent(NoticeEvent):
    @property
    def relates_to(self) -> dict[str, Any]:
        value = self.content.get("m.relates_to", {})
        return value if isinstance(value, dict) else {}

    @property
    def target_event_id(self) -> str | None:
        value = self.relates_to.get("event_id")
        return value if isinstance(value, str) else None

    @property
    def key(self) -> str | None:
        value = self.relates_to.get("key")
        return value if isinstance(value, str) else None


class RedactionEvent(NoticeEvent):
    redacts: EventId | None = None


class TypingEvent(NoticeEvent):
    @property
    def user_ids(self) -> list[UserId]:
        values = self.content.get("user_ids", [])
        if not isinstance(values, list):
            return []
        return [UserId(str(value)) for value in values]


class ReceiptEvent(NoticeEvent):
    pass


class UnknownRoomEvent(NoticeEvent):
    pass


class InviteEvent(NoticeEvent):
    pass


class LeaveEvent(NoticeEvent):
    pass


class SyncMetaEvent(MetaEvent):
    next_batch: str | None = None


event_classes: dict[str, type[Event]] = {
    "m.room.message": RoomMessageEvent,
    "m.room.encrypted": EncryptedRoomEvent,
    "m.room.member": RoomMemberEvent,
    "m.reaction": ReactionEvent,
    "m.room.redaction": RedactionEvent,
    "m.typing": TypingEvent,
    "m.receipt": ReceiptEvent,
}


def event_from_raw(
    raw: RawMatrixEvent,
    *,
    room_id: RoomId | str | None = None,
    to_me: bool = False,
) -> Event:
    data = raw.model_dump(by_alias=True, exclude_none=True)
    if room_id is not None and data.get("room_id") is None:
        data["room_id"] = str(room_id)
    data["to_me"] = to_me
    event_class = event_classes.get(raw.type, UnknownRoomEvent)
    return event_class.model_validate(data)


__all__ = (
    "EncryptedRoomEvent",
    "Event",
    "EventType",
    "InviteEvent",
    "LeaveEvent",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "ReactionEvent",
    "ReceiptEvent",
    "RedactionEvent",
    "RoomMemberEvent",
    "RoomMessageEvent",
    "SyncMetaEvent",
    "TypingEvent",
    "UnknownRoomEvent",
    "event_classes",
    "event_from_raw",
)
