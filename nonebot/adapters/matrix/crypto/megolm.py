"""libolm group sessions with origin binding, rotation and replay tracking."""

from __future__ import annotations

import json
from time import time
from typing import Any

from olm.group_session import (
    InboundGroupSession,
    OlmGroupSessionError,
    OutboundGroupSession,
)

from .primitives import MEGOLM_ALGORITHM
from .store import CryptoStore
from .types import DecryptionError


class MegolmManager:
    def __init__(self, store: CryptoStore) -> None:
        self.store = store

    def outbound(
        self, room_id: str, settings: dict[str, Any], identity: dict[str, str]
    ) -> tuple[OutboundGroupSession, dict[str, Any]]:
        record = self.store.get(f"outbound/{room_id}")
        if record is not None:
            session = OutboundGroupSession.from_pickle(
                record["pickle"].encode(), self.store.pickle_key
            )
            period = max(
                3600_000,
                min(int(settings.get("rotation_period_ms", 604800_000)), 604800_000),
            )
            limit = max(1, min(int(settings.get("rotation_period_msgs", 100)), 10000))
            if (
                time() - record["created"]
            ) * 1000 < period and session.message_index < limit:
                return session, record
        session = OutboundGroupSession()
        record = {
            "created": time(),
            "shared": {},
            "recipients": [],
            "history_visibility": settings.get("history_visibility", "shared"),
        }
        with self.store.transaction():
            self.save_outbound(room_id, session, record)
            self.import_key(
                {
                    "room_id": room_id,
                    "session_id": session.id,
                    "session_key": session.session_key,
                    "sender_key": identity["curve25519"],
                    "sender_claimed_keys": {"ed25519": identity["ed25519"]},
                    "sender": identity["user_id"],
                    "forwarding_curve25519_key_chain": [],
                },
                exported=False,
            )
        return session, record

    def save_outbound(
        self, room_id: str, session: OutboundGroupSession, record: dict[str, Any]
    ) -> None:
        record["pickle"] = session.pickle(self.store.pickle_key).decode()
        self.store.put(f"outbound/{room_id}", record)

    def discard(self, room_id: str) -> None:
        self.store.delete(f"outbound/{room_id}")

    def import_key(self, value: dict[str, Any], *, exported: bool = True) -> bool:
        if value.get("algorithm", MEGOLM_ALGORITHM) != MEGOLM_ALGORITHM:
            raise DecryptionError("UnsupportedAlgorithm")
        session = (
            InboundGroupSession.import_session(value["session_key"])
            if exported
            else InboundGroupSession(value["session_key"])
        )
        if (
            session.id != value["session_id"]
            or not value.get("sender_key")
            or not value.get("sender_claimed_keys", {}).get("ed25519")
        ):
            raise DecryptionError("InvalidRoomKey")
        name = f"inbound/{value['room_id']}/{session.id}"
        previous = self.store.get(name)
        if previous:
            if (
                previous["sender_key"] != value["sender_key"]
                or previous["sender_claimed_keys"] != value["sender_claimed_keys"]
            ):
                raise DecryptionError("RoomKeyOriginMismatch")
            old = InboundGroupSession.from_pickle(
                previous["pickle"].encode(), self.store.pickle_key
            )
            if old.first_known_index <= session.first_known_index:
                return False
        record = {k: v for k, v in value.items() if k != "session_key"}
        record["algorithm"] = MEGOLM_ALGORITHM
        record["pickle"] = session.pickle(self.store.pickle_key).decode()
        record["first_known_index"] = session.first_known_index
        record["backed_up"] = None
        self.store.put(name, record)
        return True

    def export_keys(
        self, room_id: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        values = []
        for name in self.store.names("inbound/"):
            record = self.store.get(name)
            if room_id is not None and record["room_id"] != room_id:
                continue
            if session_id is not None and record["session_id"] != session_id:
                continue
            session = InboundGroupSession.from_pickle(
                record["pickle"].encode(), self.store.pickle_key
            )
            values.append(
                {
                    **{
                        k: v
                        for k, v in record.items()
                        if k not in {"pickle", "backed_up"}
                    },
                    "session_key": session.export_session(session.first_known_index),
                }
            )
        return values

    def decrypt(self, room_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        content = raw["content"]
        if content.get("algorithm") != MEGOLM_ALGORITHM:
            raise DecryptionError("UnsupportedAlgorithm")
        session_id = content.get("session_id", "")
        name = f"inbound/{room_id}/{session_id}"
        record = self.store.get(name)
        if record is None:
            raise DecryptionError("MissingRoomKey")
        if content.get("sender_key") and record["sender_key"] != content["sender_key"]:
            raise DecryptionError("SenderKeyMismatch")
        if record.get("sender") and raw.get("sender") != record["sender"]:
            raise DecryptionError("SenderMismatch")
        session = InboundGroupSession.from_pickle(
            record["pickle"].encode(), self.store.pickle_key
        )
        try:
            text, index = session.decrypt(
                content["ciphertext"], unicode_errors="strict"
            )
            decrypted = json.loads(text)
        except (OlmGroupSessionError, ValueError, KeyError) as exc:
            raise DecryptionError("InvalidMegolmMessage") from exc
        if (
            not isinstance(decrypted, dict)
            or decrypted.get("room_id") != room_id
            or not isinstance(decrypted.get("type"), str)
            or not isinstance(decrypted.get("content"), dict)
        ):
            raise DecryptionError("InvalidRoomPayload")
        replay_name = f"replay/{room_id}/{session_id}/{index}"
        event = [raw.get("event_id"), raw.get("origin_server_ts")]
        previous = self.store.get(replay_name)
        if previous is not None and previous != event:
            raise DecryptionError("ReplayedMessage")
        with self.store.transaction():
            self.store.put(replay_name, event)
            record["pickle"] = session.pickle(self.store.pickle_key).decode()
            self.store.put(name, record)
        return {
            **raw,
            "type": decrypted["type"],
            "content": decrypted["content"],
            "encrypted_content": content,
        }
