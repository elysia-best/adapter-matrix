from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .types import DeviceId, EventId, MxcUri, RoomId, UserId


class MatrixBaseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow", populate_by_name=True, arbitrary_types_allowed=True
    )


class MatrixError(MatrixBaseModel):
    errcode: str | None = None
    error: str | None = None
    retry_after_ms: int | None = None


class File(MatrixBaseModel):
    filename: str
    content: bytes
    content_type: str | None = None


class LoginIdentifier(MatrixBaseModel):
    type: str = "m.id.user"
    user: str | None = None
    medium: str | None = None
    address: str | None = None
    country: str | None = None
    phone: str | None = None


class PasswordLoginRequest(MatrixBaseModel):
    type: str = "m.login.password"
    identifier: LoginIdentifier | None = None
    user: str | None = None
    password: str
    device_id: DeviceId | None = None
    initial_device_display_name: str | None = None
    refresh_token: bool | None = None


class LoginResponse(MatrixBaseModel):
    user_id: UserId
    access_token: str | None = None
    device_id: DeviceId | None = None
    well_known: dict[str, Any] | None = Field(default=None, alias="well_known")
    expires_in_ms: int | None = None
    refresh_token: str | None = None


class RefreshTokenRequest(MatrixBaseModel):
    refresh_token: str


class RefreshTokenResponse(MatrixBaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in_ms: int | None = None


class LoginFlow(MatrixBaseModel):
    type: str


class LoginFlowsResponse(MatrixBaseModel):
    flows: list[LoginFlow] = Field(default_factory=list)


class WhoamiResponse(MatrixBaseModel):
    user_id: UserId
    device_id: DeviceId | None = None
    is_guest: bool | None = None


class EventIdResponse(MatrixBaseModel):
    event_id: EventId


class UploadResponse(MatrixBaseModel):
    content_uri: MxcUri


class MediaConfigResponse(MatrixBaseModel):
    upload_size: int | None = Field(default=None, alias="m.upload.size")


class UnsignedData(MatrixBaseModel):
    age: int | None = None
    membership: str | None = None
    prev_content: dict[str, Any] | None = None
    redacted_because: dict[str, Any] | None = None
    transaction_id: str | None = None
    relations: dict[str, Any] | None = Field(default=None, alias="m.relations")


class RawMatrixEvent(MatrixBaseModel):
    type: str
    content: dict[str, Any] = Field(default_factory=dict)
    event_id: EventId | None = None
    sender: UserId | None = None
    room_id: RoomId | None = None
    origin_server_ts: int | None = None
    state_key: str | None = None
    redacts: EventId | None = None
    unsigned: UnsignedData | dict[str, Any] | None = None


class Timeline(MatrixBaseModel):
    events: list[RawMatrixEvent] = Field(default_factory=list)
    limited: bool | None = None
    prev_batch: str | None = None


class State(MatrixBaseModel):
    events: list[RawMatrixEvent] = Field(default_factory=list)


class Ephemeral(MatrixBaseModel):
    events: list[RawMatrixEvent] = Field(default_factory=list)


class AccountData(MatrixBaseModel):
    events: list[RawMatrixEvent] = Field(default_factory=list)


class JoinedRoomSync(MatrixBaseModel):
    timeline: Timeline = Field(default_factory=Timeline)
    state: State = Field(default_factory=State)
    ephemeral: Ephemeral = Field(default_factory=Ephemeral)
    account_data: AccountData = Field(default_factory=AccountData)
    summary: dict[str, Any] | None = None
    unread_notifications: dict[str, Any] | None = None


class InvitedRoomSync(MatrixBaseModel):
    invite_state: State = Field(default_factory=State)


class LeftRoomSync(MatrixBaseModel):
    timeline: Timeline = Field(default_factory=Timeline)
    state: State = Field(default_factory=State)
    account_data: AccountData = Field(default_factory=AccountData)


class RoomsSync(MatrixBaseModel):
    join: dict[RoomId, JoinedRoomSync] = Field(default_factory=dict)
    invite: dict[RoomId, InvitedRoomSync] = Field(default_factory=dict)
    leave: dict[RoomId, LeftRoomSync] = Field(default_factory=dict)


class PresenceSync(MatrixBaseModel):
    events: list[RawMatrixEvent] = Field(default_factory=list)


class DeviceLists(MatrixBaseModel):
    changed: list[UserId] = Field(default_factory=list)
    left: list[UserId] = Field(default_factory=list)


class SyncResponse(MatrixBaseModel):
    next_batch: str
    rooms: RoomsSync = Field(default_factory=RoomsSync)
    presence: PresenceSync = Field(default_factory=PresenceSync)
    account_data: AccountData = Field(default_factory=AccountData)
    device_lists: DeviceLists | None = None
    to_device: AccountData | None = None


class RoomMessageContent(MatrixBaseModel):
    msgtype: str
    body: str
    format: str | None = None
    formatted_body: str | None = None
    url: MxcUri | str | None = None
    info: dict[str, Any] | None = None
    relates_to: dict[str, Any] | None = Field(default=None, alias="m.relates_to")
    mentions: dict[str, Any] | None = Field(default=None, alias="m.mentions")


class RoomMemberContent(MatrixBaseModel):
    membership: str
    displayname: str | None = None
    avatar_url: MxcUri | str | None = None
    reason: str | None = None
    is_direct: bool | None = None


class ReactionRelation(MatrixBaseModel):
    rel_type: str = "m.annotation"
    event_id: EventId
    key: str


class ReactionContent(MatrixBaseModel):
    relates_to: ReactionRelation = Field(alias="m.relates_to")


class RedactionContent(MatrixBaseModel):
    reason: str | None = None


class TypingContent(MatrixBaseModel):
    user_ids: list[UserId] = Field(default_factory=list)
    typing: bool | None = None
    timeout: int | None = None


class ReceiptThread(MatrixBaseModel):
    ts: int | None = None
    thread_id: str | None = None


ReceiptContent = dict[str, dict[str, dict[UserId, ReceiptThread | dict[str, Any]]]]


class MembersChunkResponse(MatrixBaseModel):
    chunk: list[RawMatrixEvent] = Field(default_factory=list)


class MessagesResponse(MatrixBaseModel):
    chunk: list[RawMatrixEvent] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None
    state: list[RawMatrixEvent] = Field(default_factory=list)


class RelationsResponse(MatrixBaseModel):
    chunk: list[RawMatrixEvent] = Field(default_factory=list)
    next_batch: str | None = None
    prev_batch: str | None = None


class JoinRoomResponse(MatrixBaseModel):
    room_id: RoomId


# ------------------------------------------------------------------
# E2EE 模型: m.room.encrypted 事件和 to-device 密钥消息
# ------------------------------------------------------------------


class EncryptedEventContent(MatrixBaseModel):
    """m.room.encrypted 事件的内容。"""

    algorithm: str
    ciphertext: str
    sender_key: str
    session_id: str
    device_id: str | None = None


class RoomKeyContent(MatrixBaseModel):
    """m.room_key to-device 消息的内容。

    用于在设备间共享 Megolm 会话密钥。
    """

    algorithm: str
    room_id: str
    session_id: str
    session_key: str


class ForwardedRoomKeyContent(MatrixBaseModel):
    """m.forwarded_room_key to-device 消息的内容。

    包含额外的转发链信息。
    """

    algorithm: str
    room_id: str
    session_id: str
    session_key: str
    sender_key: str
    sender_claimed_ed25519_key: str
    forwarding_curve25519_chain: list[str] = Field(default_factory=list)


__all__ = (
    "AccountData",
    "DeviceLists",
    "EncryptedEventContent",
    "EventIdResponse",
    "File",
    "ForwardedRoomKeyContent",
    "InvitedRoomSync",
    "JoinRoomResponse",
    "JoinedRoomSync",
    "LeftRoomSync",
    "LoginFlow",
    "LoginFlowsResponse",
    "LoginIdentifier",
    "LoginResponse",
    "MatrixBaseModel",
    "MatrixError",
    "MediaConfigResponse",
    "MembersChunkResponse",
    "MessagesResponse",
    "PasswordLoginRequest",
    "PresenceSync",
    "RawMatrixEvent",
    "ReactionContent",
    "ReactionRelation",
    "ReceiptContent",
    "ReceiptThread",
    "RedactionContent",
    "RefreshTokenRequest",
    "RefreshTokenResponse",
    "RelationsResponse",
    "RoomKeyContent",
    "RoomMemberContent",
    "RoomMessageContent",
    "RoomsSync",
    "State",
    "SyncResponse",
    "Timeline",
    "TypingContent",
    "UnsignedData",
    "UploadResponse",
    "WhoamiResponse",
)
