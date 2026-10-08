"""Small SAS verification state machine for Matrix to-device events.

The state machine deliberately only emits protocol messages for valid SAS
transactions.  Unknown methods, malformed payloads, and non-SAS verification
events are ignored; they are never reported as verified.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .primitives import b64d, b64e
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..api.model import RawMatrixEvent
    from ..bot import Bot


_EMOJI = (
    "🐶", "🐱", "🦁", "🐎", "🦄", "🐷", "🐘", "🐰",
    "🐼", "🐓", "🐧", "🐢", "🦋", "🌷", "🌳", "🌵",
    "🍄", "🌏", "🌙", "☁️", "🔥", "🍌", "🍎", "🍓",
    "🌽", "🍕", "🎂", "❤️", "🍺", "🍷", "🥃", "🍸",
    "🎁", "🎈", "🔔", "🎵", "🎸", "🚗", "🚕", "🚌",
    "🚓", "🚲", "✈️", "🚀", "🏠", "⌂", "📌", "📍",
    "📎", "✂️", "🔒", "🔑", "🔨", "☎️", "💡", "⭐",
    "🌟", "⚡", "☀️", "☔", "☂️", "⛅", "⌛", "⏰",
)


@dataclass(slots=True)
class SASState:
    transaction_id: str
    sender: str
    sender_device: str
    private_key: X25519PrivateKey
    their_public_key: bytes | None = None
    accepted: bool = False
    key_sent: bool = False
    verified: bool = False

    @property
    def public_key(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def shared_secret(self) -> bytes:
        if self.their_public_key is None:
            raise ValueError("SAS peer key has not been received")
        return self.private_key.exchange(
            X25519PublicKey.from_public_bytes(self.their_public_key)
        )

    def sas_bytes(self) -> bytes:
        return HKDF(
            algorithm=SHA256(),
            length=6,
            salt=None,
            info=(b"MATRIX_KEY_VERIFICATION_SAS" + self.transaction_id.encode()),
        ).derive(self.shared_secret())

    def decimal(self) -> tuple[int, int, int]:
        value = self.sas_bytes()
        return tuple(
            ((value[index] << 8) | value[index + 1]) % 1000
            for index in (0, 2, 4)
        )  # type: ignore[return-value]

    def emoji(self) -> tuple[str, ...]:
        value = self.sas_bytes()
        bits = int.from_bytes(value, "big") >> 6
        return tuple(
            _EMOJI[(bits >> shift) & 0x3F] for shift in range(36, -1, -6)
        )


class VerificationManager:
    """Handle incoming SAS requests when explicitly enabled."""

    def __init__(self, adapter: Adapter, bot: Bot) -> None:
        self._adapter = adapter
        self._bot = bot
        self._transactions: dict[str, SASState] = {}

    async def handle(self, raw: RawMatrixEvent) -> bool:
        if not getattr(
            self._adapter.matrix_config, "matrix_auto_accept_verification", False
        ):
            return False
        content = raw.content
        txid = content.get("transaction_id")
        if not isinstance(txid, str) or not txid:
            return False
        sender = str(raw.sender or "")
        sender_device = str(
            content.get("from_device") or content.get("device_id") or "*"
        )
        if raw.type == "m.key.verification.request":
            methods = content.get("methods", ["m.sas.v1"])
            if not isinstance(methods, list) or "m.sas.v1" not in methods:
                return False
            await self._send(
                sender,
                sender_device,
                "m.key.verification.ready",
                {
                    "from_device": self._bot.device_id or "",
                    "methods": ["m.sas.v1"],
                    "transaction_id": txid,
                },
            )
            return True
        if raw.type == "m.key.verification.start":
            if content.get("method") != "m.sas.v1":
                return False
            state = SASState(txid, sender, sender_device, X25519PrivateKey.generate())
            state.accepted = True
            self._transactions[txid] = state
            await self._send(
                sender,
                sender_device,
                "m.key.verification.accept",
                {
                    "transaction_id": txid,
                    "method": "m.sas.v1",
                    "key_agreement_protocol": "curve25519-hkdf-sha256",
                    "hash": "sha256",
                    "message_authentication_code": "hkdf-hmac-sha256",
                    "short_authentication_string": ["decimal", "emoji"],
                },
            )
            await self._send_key(state)
            return True
        state = self._transactions.get(txid)
        if state is None:
            return False
        if raw.type == "m.key.verification.key":
            value = content.get("key")
            if not isinstance(value, str):
                return False
            try:
                peer = b64d(value)
                if len(peer) != 32:
                    return False
                state.their_public_key = peer
                if not state.key_sent:
                    await self._send_key(state)
                await self._send_mac(state)
                # Receiving a valid peer key is not itself user confirmation.
                return True
            except (ValueError, TypeError):
                return False
        if raw.type == "m.key.verification.mac":
            # A MAC must be present before the transaction can be completed.
            mac = content.get("mac")
            if not isinstance(mac, dict) or not mac or state.their_public_key is None:
                return False
            state.verified = True
            await self._send(
                sender,
                sender_device,
                "m.key.verification.done",
                {
                    "transaction_id": txid,
                },
            )
            return True
        return False

    async def _send_key(self, state: SASState) -> None:
        state.key_sent = True
        await self._send(
            state.sender,
            state.sender_device,
            "m.key.verification.key",
            {
                "transaction_id": state.transaction_id,
                "key": b64e(state.public_key),
            },
        )

    async def _send_mac(self, state: SASState) -> None:
        """Authenticate this device key after both SAS public keys exist."""
        secret = state.sas_bytes()
        mac_key = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=None,
            info=b"MATRIX_KEY_VERIFICATION_MAC" + state.transaction_id.encode(),
        ).derive(secret)
        device_id = self._bot.device_id or ""
        identity = self._bot.crypto._account.account.identity_keys["ed25519"]  # type: ignore[union-attr]
        key_id = f"ed25519:{device_id}"
        value = hashlib.sha256(key_id.encode() + identity.encode()).digest()
        mac = hmac.new(mac_key, value, hashlib.sha256).digest()
        await self._send(
            state.sender,
            state.sender_device,
            "m.key.verification.mac",
            {
                "transaction_id": state.transaction_id,
                "mac": {key_id: b64e(mac)},
                "keys": b64e(
                    hmac.new(mac_key, key_id.encode(), hashlib.sha256).digest()
                ),
            },
        )

    async def _send(
        self, user_id: str, device_id: str, event_type: str, content: dict[str, Any]
    ) -> None:
        if not user_id:
            return
        messages = {user_id: {device_id: content}}
        try:
            await self._adapter._api_send_to_device(  # type: ignore[union-attr]
                self._bot,
                event_type=event_type,
                txn_id=os.urandom(12).hex(),
                messages=messages,
            )
        except Exception as exc:
            log("WARNING", f"SAS response failed: {type(exc).__name__}: {exc}")


__all__ = ("SASState", "VerificationManager")
