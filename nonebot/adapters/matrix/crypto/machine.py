"""Network-independent crypto state machine facade.

The adapter can feed sync data into this object and execute the returned request
records through any transport.  ``CryptoEngine`` remains the integration layer
for the existing NoneBot lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .megolm import MegolmManager
from .store import CryptoStore


@dataclass(slots=True)
class OutgoingRequest:
    request_id: str
    kind: str
    body: dict[str, Any]


@dataclass(slots=True)
class SyncChanges:
    to_device_events: list[dict[str, Any]] = field(default_factory=list)
    changed_users: list[str] = field(default_factory=list)
    left_users: list[str] = field(default_factory=list)
    one_time_key_counts: dict[str, int] = field(default_factory=dict)
    next_batch: str | None = None


class CryptoMachine:
    """Sans-I/O facade around the adapter's crypto state."""

    def __init__(self, store: CryptoStore, megolm: MegolmManager) -> None:
        self.store = store
        self.megolm = megolm
        self.pending: list[OutgoingRequest] = []
        self.sync_token: str | None = None

    def receive_sync_changes(self, changes: SyncChanges) -> list[dict[str, Any]]:
        self.sync_token = changes.next_batch or self.sync_token
        return list(changes.to_device_events)

    def outgoing_requests(self) -> list[OutgoingRequest]:
        return list(self.pending)

    def mark_request_as_sent(self, request_id: str) -> None:
        self.pending = [
            request for request in self.pending if request.request_id != request_id
        ]

    def add_room_key(self, room_id: str, session_id: str, session_key: str) -> bool:
        return self.megolm.add_inbound_session(room_id, session_id, session_key)

    def encrypt_room_event(
        self, room_id: str, event_type: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        plaintext = {"type": event_type, "content": content, "room_id": room_id}
        return self.megolm.encrypt(
            room_id, __import__("json").dumps(plaintext, separators=(",", ":"))
        )

    def decrypt_room_event(
        self, room_id: str, encrypted: dict[str, Any]
    ) -> dict[str, Any] | None:
        plaintext = self.megolm.decrypt(
            room_id,
            str(encrypted.get("session_id", "")),
            str(encrypted.get("ciphertext", "")),
        )
        if plaintext is None:
            return None
        value = __import__("json").loads(plaintext)
        return value if isinstance(value, dict) else None
