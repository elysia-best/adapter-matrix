"""The room-facing subset of the Matrix SDK encryption API."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .crypto.primitives import MEGOLM_ALGORITHM
from .crypto.types import CryptoError


class Room:
    def __init__(self, bot: Any, room_id: str) -> None:
        self.bot, self.room_id = bot, room_id

    def state(self) -> str:
        return self.bot._rooms[self.room_id]

    def _ensure_joined(self) -> None:
        if self.state() != "join":
            raise CryptoError("Sending requires a joined room")

    async def is_encrypted(self) -> bool:
        return bool(await self.bot._room_encryption_settings(self.room_id))

    async def enable_encryption(self) -> Any:
        self._ensure_joined()
        existing = await self.bot._room_encryption_settings(self.room_id)
        if existing:
            if existing.get("algorithm") != MEGOLM_ALGORITHM:
                raise CryptoError(
                    "Room already uses an unsupported encryption algorithm"
                )
            return None
        content = {"algorithm": MEGOLM_ALGORITHM}
        result = await self.bot.send_state_event(
            room_id=self.room_id,
            event_type="m.room.encryption",
            state_key="",
            content=content,
        )
        self.bot._encryption_states[self.room_id] = content
        return result

    async def send(self, message: Any, **kwargs: Any) -> Any:
        self._ensure_joined()
        return await self.bot.send_to(self.room_id, message, **kwargs)

    async def send_raw(
        self, event_type: str, content: dict[str, Any], *, txn_id: str | None = None
    ) -> Any:
        self._ensure_joined()
        return await self.bot.send_event(
            room_id=self.room_id,
            event_type=event_type,
            content=content,
            txn_id=txn_id or uuid4().hex,
        )

    async def send_attachment(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> Any:
        from .message import MessageSegment

        return await self.send(
            MessageSegment.file(data, filename=filename, content_type=content_type)
        )

    async def decrypt_event(self, event: Any) -> Any:
        return await self.bot.encryption().decrypt_room_event(
            event, room_id=self.room_id
        )

    async def discard_room_key(self) -> None:
        self.bot.encryption().invalidate_outbound_sessions(self.room_id)

    async def contains_only_verified_devices(self) -> bool:
        engine = self.bot.encryption()
        members = await engine.call(
            "get_room_members", room_id=self.room_id, membership="join"
        )
        users = [str(event.state_key) for event in members.chunk if event.state_key]
        await engine.devices.query(engine, users)
        return all(
            engine.devices.is_verified(user, device)
            for user in users
            for device in engine.devices.get_device_keys_for_user(user)
        )
