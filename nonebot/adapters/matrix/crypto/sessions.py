"""Sender-bound multi-session Olm transport using libolm pickles."""

from __future__ import annotations

from time import time_ns
from typing import Any

from olm.session import (
    InboundSession,
    OlmMessage,
    OlmPreKeyMessage,
    OlmSessionError,
    OutboundSession,
    Session,
)

from .account import OlmAccountManager
from .store import CryptoStore
from .types import DecryptionError


class OlmSessionManager:
    def __init__(self, store: CryptoStore, account_mgr: OlmAccountManager) -> None:
        self.store = store
        self.account = account_mgr

    def _records(self, sender_key: str) -> list[tuple[str, dict[str, Any]]]:
        records = [(name, self.store.get(name)) for name in self.store.names("olm/")]
        return sorted(
            (
                (name, record)
                for name, record in records
                if record["sender_key"] == sender_key
            ),
            key=lambda pair: pair[1]["last_used"],
        )

    def _save(self, session: Session, sender_key: str) -> None:
        self.store.put(
            f"olm/{session.id}",
            {
                "sender_key": sender_key,
                "last_used": time_ns(),
                "pickle": session.pickle(self.store.pickle_key).decode("ascii"),
            },
        )

    def get(self, sender_key: str) -> Session | None:
        records = self._records(sender_key)
        if not records:
            return None
        return Session.from_pickle(
            records[-1][1]["pickle"].encode("ascii"), self.store.pickle_key
        )

    def create_outbound_session(self, sender_key: str, one_time_key: str) -> Session:
        session = OutboundSession(self.account.account, sender_key, one_time_key)
        self._save(session, sender_key)
        return session

    def encrypt(
        self, session: Session, sender_key: str, plaintext: str
    ) -> dict[str, Any]:
        message = session.encrypt(plaintext)
        self._save(session, sender_key)
        return {"type": message.message_type, "body": message.ciphertext}

    def decrypt_to_device_message(
        self, ciphertext: str, message_type: int, sender_key: str
    ) -> str:
        if message_type not in (0, 1) or not sender_key:
            raise DecryptionError("InvalidOlmMessage")
        message = (
            OlmPreKeyMessage(ciphertext)
            if message_type == 0
            else OlmMessage(ciphertext)
        )
        for _, record in reversed(self._records(sender_key)):
            session = Session.from_pickle(
                record["pickle"].encode("ascii"), self.store.pickle_key
            )
            try:
                if isinstance(message, OlmPreKeyMessage) and not session.matches(
                    message, sender_key
                ):
                    continue
                plaintext = session.decrypt(message, unicode_errors="strict")
            except OlmSessionError:
                continue
            self._save(session, sender_key)
            return plaintext
        if isinstance(message, OlmPreKeyMessage):
            try:
                session = InboundSession(self.account.account, message, sender_key)
                plaintext = session.decrypt(message, unicode_errors="strict")
                with self.store.transaction():
                    self.account.account.remove_one_time_keys(session)
                    self.account.save()
                    self._save(session, sender_key)
                return plaintext
            except OlmSessionError as exc:
                raise DecryptionError("InvalidOlmMessage") from exc
        raise DecryptionError("MissingOlmSession")
