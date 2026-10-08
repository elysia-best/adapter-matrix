"""Pure Python room group sessions."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .device_keys import DeviceKeyStore
from .primitives import aes_cbc_decrypt, aes_cbc_encrypt, b64d, b64e, megolm_keys
from .sessions import OlmSessionManager
from .store import CryptoStore
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError


@dataclass(slots=True)
class GroupSession:
    session_id: str
    session_key: str
    message_index: int = 0
    ratchet: bytes = b""
    ed25519_private: bytes = b""
    first_known_index: int = 0

    def __post_init__(self) -> None:
        if self.ratchet or not self.session_key:
            return
        decoded = b64d(self.session_key)
        if len(decoded) in (165, 229) and decoded[0] in (1, 2):
            self.first_known_index = int.from_bytes(decoded[1:5], "big")
            self.message_index = max(self.message_index, self.first_known_index)
            self.ratchet = decoded[5:133]
            if not self.session_id:
                self.session_id = b64e(decoded[133:165])

    @property
    def id(self) -> str:
        return self.session_id

    @classmethod
    def create(cls) -> GroupSession:
        private = Ed25519PrivateKey.generate()
        private_bytes = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        ratchet = os.urandom(128)
        session = cls(b64e(public), "", 0, ratchet, private_bytes, 0)
        session.session_key = session.sharing_key()
        return session

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GroupSession:
        session_key = str(value["session_key"])
        try:
            decoded = b64d(session_key)
            if decoded[:1] in (b"\x01", b"\x02") and len(decoded) >= 165:
                index = int.from_bytes(decoded[1:5], "big")
                ratchet = decoded[5:133]
                session_id = b64e(decoded[133:165])
            else:
                index, ratchet, session_id = 0, b"", str(value["session_id"])
        except (ValueError, TypeError):
            index, ratchet, session_id = 0, b"", str(value["session_id"])
        return cls(
            session_id,
            session_key,
            int(value.get("message_index", index)),
            ratchet,
            b64d(str(value.get("ed25519_private", "")))
            if value.get("ed25519_private")
            else b"",
            index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_key": self.session_key,
            "message_index": self.message_index,
            "ed25519_private": b64e(self.ed25519_private)
            if self.ed25519_private
            else "",
            "first_known_index": self.first_known_index,
        }

    def export_key(self) -> str:
        return b64e(
            b"\x01"
            + self.message_index.to_bytes(4, "big")
            + self.ratchet
            + b64d(self.session_id)
        )

    def sharing_key(self) -> str:
        body = (
            b"\x02"
            + self.message_index.to_bytes(4, "big")
            + self.ratchet
            + b64d(self.session_id)
        )
        signature = Ed25519PrivateKey.from_private_bytes(self.ed25519_private).sign(
            body
        )
        return b64e(body + signature)

    def _ratchet_at(self, index: int) -> bytes:
        if index < self.first_known_index:
            raise ValueError
        value = self.ratchet
        current = self.first_known_index
        while current < index:
            old = [value[i * 32 : (i + 1) * 32] for i in range(4)]
            if (current + 1) % (1 << 24) == 0:
                old[1] = hmac.new(old[0], b"\x01", "sha256").digest()
                old[2] = hmac.new(old[0], b"\x02", "sha256").digest()
                old[3] = hmac.new(old[0], b"\x03", "sha256").digest()
            elif (current + 1) % (1 << 16) == 0:
                old[2] = hmac.new(old[1], b"\x02", "sha256").digest()
                old[3] = hmac.new(old[1], b"\x03", "sha256").digest()
            elif (current + 1) % (1 << 8) == 0:
                old[3] = hmac.new(old[2], b"\x03", "sha256").digest()
            else:
                old[3] = hmac.new(old[3], b"\x03", "sha256").digest()
            value = b"".join(old)
            current += 1
        return value

    def encrypt(self, plaintext: str) -> str:
        index = self.message_index
        ratchet = self._ratchet_at(index)
        aes_key, mac_key, iv = megolm_keys(ratchet)
        ciphertext = aes_cbc_encrypt(aes_key, iv, plaintext.encode())
        payload = (
            b"\x08" + _varint(index) + b"\x12" + _varint(len(ciphertext)) + ciphertext
        )
        authenticated = b"\x03" + payload
        mac = hmac.new(mac_key, authenticated, "sha256").digest()[:8]
        signature = Ed25519PrivateKey.from_private_bytes(self.ed25519_private).sign(
            authenticated + mac
        )
        self.message_index += 1
        return b64e(authenticated + mac + signature)

    def decrypt(self, ciphertext: str) -> tuple[str, int]:
        encoded = b64d(ciphertext)
        if len(encoded) < 73 or encoded[0] != 3:
            raise ValueError
        payload, mac, signature = encoded[:-72], encoded[-72:-64], encoded[-64:]
        tag, offset = _read_varint(payload, 1)
        if tag != 0x08:
            raise ValueError
        index, offset = _read_varint(payload, offset)
        if payload[offset] != 0x12:
            raise ValueError
        length, offset = _read_varint(payload, offset + 1)
        encrypted = payload[offset : offset + length]
        ratchet = self._ratchet_at(index)
        aes_key, mac_key, iv = megolm_keys(ratchet)
        if not hmac.compare_digest(
            mac, hmac.new(mac_key, payload, "sha256").digest()[:8]
        ):
            raise ValueError
        Ed25519PublicKey.from_public_bytes(b64d(self.session_id)).verify(
            signature, payload + mac
        )
        return aes_cbc_decrypt(aes_key, iv, encrypted).decode("utf-8"), index


class MegolmManager:
    """Manage room sessions and encrypted room event payloads."""

    def __init__(
        self,
        store: CryptoStore,
        session_mgr: OlmSessionManager,
        device_keys: DeviceKeyStore,
    ) -> None:
        self._store = store
        self._session_mgr = session_mgr
        self._device_keys = device_keys
        self._inbound: dict[str, dict[str, GroupSession]] = {}
        self._outbound: dict[str, GroupSession] = {}

    def load(self) -> None:
        self._inbound = {}
        for room_id, sessions in self._store.load_inbound_sessions().items():
            self._inbound[room_id] = {}
            for session_id, session_key in sessions.items():
                if isinstance(session_key, str):
                    try:
                        decoded = b64d(session_key)
                        if decoded[:1] in (b"\x01", b"\x02") and len(decoded) in (
                            165,
                            229,
                        ):
                            if decoded[0] == 2:
                                Ed25519PublicKey.from_public_bytes(
                                    decoded[133:165]
                                ).verify(decoded[165:], decoded[:165])
                            self._inbound[room_id][session_id] = GroupSession(
                                b64e(decoded[133:165]),
                                session_key,
                                int.from_bytes(decoded[1:5], "big"),
                                decoded[5:133],
                                b"",
                                int.from_bytes(decoded[1:5], "big"),
                            )
                    except (InvalidSignature, ValueError, TypeError):
                        continue
        self._outbound = {
            room_id: GroupSession.from_dict(value)
            for room_id, value in self._store.load_outbound_sessions().items()
            if isinstance(value, dict)
            and "session_id" in value
            and "session_key" in value
        }

    def get_outbound_session(self, room_id: str) -> GroupSession:
        session = self._outbound.get(room_id)
        if session is None:
            session = GroupSession.create()
            self._outbound[room_id] = session
            self._save_outbound()
        return session

    def get_outbound_session_id(self, room_id: str) -> str:
        return self.get_outbound_session(room_id).session_id

    def encrypt(self, room_id: str, plaintext: str) -> dict[str, str]:
        session = self.get_outbound_session(room_id)
        ciphertext = session.encrypt(plaintext)
        self._save_outbound()
        return {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": ciphertext,
            "sender_key": self._session_mgr._account_mgr.account.identity_keys[
                "curve25519"
            ],
            "session_id": session.session_id,
        }

    def rotate_outbound_session(self, room_id: str) -> GroupSession:
        session = GroupSession.create()
        self._outbound[room_id] = session
        self._save_outbound()
        return session

    def add_inbound_session(
        self, room_id: str, session_id: str, session_key: str
    ) -> bool:
        try:
            decoded = b64d(session_key)
            if len(decoded) not in (165, 229) or decoded[0] not in (1, 2):
                return False
            if b64e(decoded[133:165]) != session_id:
                return False
            if decoded[0] == 2:
                Ed25519PublicKey.from_public_bytes(decoded[133:165]).verify(
                    decoded[165:], decoded[:165]
                )
        except (InvalidSignature, ValueError, TypeError):
            return False
        index = int.from_bytes(decoded[1:5], "big")
        self._inbound.setdefault(room_id, {})[session_id] = GroupSession(
            session_id, session_key, index, decoded[5:133], b"", index
        )
        self._save_inbound()
        return True

    def decrypt(self, room_id: str, session_id: str, ciphertext: str) -> str | None:
        session = self._inbound.get(room_id, {}).get(session_id)
        if session is None:
            return None
        try:
            plaintext, _index = session.decrypt(ciphertext)
            return plaintext
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            log(
                "WARNING",
                f"Megolm record authentication failed for {room_id}/{session_id}",
            )
            return None

    async def share_session_key(
        self,
        adapter: Adapter,
        bot: Bot,
        room_id: str,
        device_id: str,
        members: list[str],
    ) -> int:
        del device_id
        session = self.get_outbound_session(room_id)
        account = self._session_mgr._account_mgr.account
        my_curve = account.identity_keys["curve25519"]
        my_ed25519 = account.identity_keys["ed25519"]
        device_keys = self._session_mgr._account_mgr.build_device_keys(bot)
        messages: dict[str, dict[str, dict[str, Any]]] = {}
        for user_id in members:
            if user_id == str(bot.user_id):
                continue
            for remote_device, remote in self._device_keys.get_device_keys_for_user(
                user_id
            ).items():
                remote_curve = remote.get("keys", {}).get(f"curve25519:{remote_device}")
                if not isinstance(remote_curve, str):
                    continue
                remote_key = await self._device_keys.claim_one_time_key(
                    adapter, bot, user_id, remote_device
                )
                if remote_key is None:
                    continue
                olm_session = self._session_mgr.create_outbound_session(
                    remote_curve, remote_key
                )
                if olm_session is None:
                    continue
                payload = {
                    "type": "m.room_key",
                    "content": {
                        "algorithm": "m.megolm.v1.aes-sha2",
                        "room_id": room_id,
                        "session_id": session.session_id,
                        "session_key": session.session_key,
                    },
                    "sender": str(bot.user_id),
                    "sender_device": bot.device_id,
                    "keys": {"ed25519": my_ed25519},
                    "recipient": user_id,
                    "recipient_keys": {
                        "ed25519": remote.get("keys", {}).get(
                            f"ed25519:{remote_device}", ""
                        )
                    },
                    "sender_device_keys": device_keys,
                }
                encrypted = self._session_mgr.encrypt(
                    olm_session, json.dumps(payload, separators=(",", ":"))
                )
                messages.setdefault(user_id, {})[remote_device] = {
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": my_curve,
                    "ciphertext": {
                        remote_curve: {
                            "body": encrypted.ciphertext,
                            "type": encrypted.message_type,
                        }
                    },
                }
        if not messages:
            return 0
        await adapter._api_send_to_device(
            bot,
            event_type="m.room.encrypted",
            txn_id=os.urandom(12).hex(),
            messages=messages,
        )
        return sum(len(devices) for devices in messages.values())

    def _save_inbound(self) -> None:
        self._store.save_inbound_sessions(
            {
                room: {sid: session.session_key for sid, session in sessions.items()}
                for room, sessions in self._inbound.items()
            }
        )

    def _save_outbound(self) -> None:
        self._store.save_outbound_sessions(
            {room: session.to_dict() for room, session in self._outbound.items()}
        )
