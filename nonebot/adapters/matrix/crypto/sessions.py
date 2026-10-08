"""Pure Python implementation of the Matrix Olm v1 wire protocol."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from .account import OlmAccountManager
from .primitives import (
    IdentityAccount,
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    b64d,
    b64e,
    hkdf,
    olm_keys,
)
from .store import CryptoStore
from ..utils import log


@dataclass(slots=True)
class PureOlmMessage:
    ciphertext: str
    message_type: int


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
    msg = "invalid Olm varint"
    raise ValueError(msg)


def _field(tag: int, value: bytes | int) -> bytes:
    return _varint(tag) + (
        _varint(value) if isinstance(value, int) else _varint(len(value)) + value
    )


def _fields(data: bytes) -> dict[int, bytes | int]:
    result: dict[int, bytes | int] = {}
    offset = 1 if data[:1] == b"\x03" else 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        if tag & 7 == 0:
            result[tag], offset = _read_varint(data, offset)
        elif tag & 7 == 2:
            length, offset = _read_varint(data, offset)
            result[tag] = data[offset : offset + length]
            offset += length
        else:
            msg = "invalid Olm field"
            raise ValueError(msg)
    return result


def _public(private: X25519PrivateKey) -> str:
    return b64e(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


class PureOlmSession:
    def __init__(
        self,
        *,
        session_id: str,
        key_material: bytes,
        sender_key: str,
        recipient_key: str,
        identity_key: str = "",
        base_key: str = "",
        chain_key: bytes = b"",
        ratchet_key: str = "",
        message_count: int = 0,
    ) -> None:
        self.id, self.key_material = session_id, key_material
        self.sender_key, self.recipient_key = sender_key, recipient_key
        self.identity_key, self.base_key = identity_key, base_key
        self.chain_key, self.ratchet_key = chain_key, ratchet_key
        self.message_count = message_count

    @classmethod
    def outbound(
        cls, account: IdentityAccount, their_identity_key: str, their_one_time_key: str
    ) -> PureOlmSession:
        base = X25519PrivateKey.generate()
        local_identity = X25519PrivateKey.from_private_bytes(account.curve25519_private)
        remote_identity = X25519PublicKey.from_public_bytes(b64d(their_identity_key))
        remote_otk = X25519PublicKey.from_public_bytes(b64d(their_one_time_key))
        shared = (
            local_identity.exchange(remote_otk)
            + base.exchange(remote_identity)
            + base.exchange(remote_otk)
        )
        root_chain = hkdf(shared, info=b"OLM_ROOT", length=64)
        identity_key = account.identity_keys["curve25519"]
        return cls(
            session_id=hashlib.sha256(
                root_chain + b64d(their_identity_key)
            ).hexdigest()[:32],
            key_material=root_chain,
            sender_key=identity_key,
            recipient_key=their_one_time_key,
            identity_key=identity_key,
            base_key=_public(base),
            chain_key=root_chain[32:],
            ratchet_key=_public(X25519PrivateKey.generate()),
        )

    @classmethod
    def inbound(
        cls, account: IdentityAccount, payload: dict[str, Any]
    ) -> PureOlmSession:
        identity_key, one_time_key, base_key = (
            str(payload["identity_key"]),
            str(payload["one_time_key"]),
            str(payload["base_key"]),
        )
        found = account.private_for_one_time_key(b64d(one_time_key))
        if found is None:
            msg = "recipient one-time key is not owned by this account"
            raise ValueError(msg)
        _, one_time_private = found
        local_identity = X25519PrivateKey.from_private_bytes(account.curve25519_private)
        one_time = X25519PrivateKey.from_private_bytes(one_time_private)
        sender_identity = X25519PublicKey.from_public_bytes(b64d(identity_key))
        sender_base = X25519PublicKey.from_public_bytes(b64d(base_key))
        shared = (
            one_time.exchange(sender_identity)
            + local_identity.exchange(sender_base)
            + one_time.exchange(sender_base)
        )
        root_chain = hkdf(shared, info=b"OLM_ROOT", length=64)
        inner = _fields(b64d(str(payload["message"]))[:-8])
        return cls(
            session_id=str(payload["session_id"]),
            key_material=root_chain,
            sender_key=identity_key,
            recipient_key=one_time_key,
            identity_key=identity_key,
            base_key=base_key,
            chain_key=root_chain[32:],
            ratchet_key=b64e(bytes(inner[0x0A])),
        )

    def encrypt(self, plaintext: str) -> PureOlmMessage:
        message_key = hmac.new(self.chain_key, b"\x01", hashlib.sha256).digest()
        self.chain_key = hmac.new(self.chain_key, b"\x02", hashlib.sha256).digest()
        aes_key, mac_key, iv = olm_keys(message_key)
        ciphertext = aes_cbc_encrypt(aes_key, iv, plaintext.encode())
        inner = (
            b"\x03"
            + _field(0x0A, b64d(self.ratchet_key))
            + _field(0x10, self.message_count)
            + _field(0x22, ciphertext)
        )
        inner += hmac.new(mac_key, inner, hashlib.sha256).digest()[:8]
        if self.message_count == 0:
            outer = (
                b"\x03"
                + _field(0x0A, b64d(self.recipient_key))
                + _field(0x12, b64d(self.base_key))
            )
            outer += _field(0x1A, b64d(self.identity_key)) + _field(0x22, inner)
            message_type = 0
        else:
            outer = inner
            message_type = 1
        self.message_count += 1
        return PureOlmMessage(b64e(outer), message_type)

    def decrypt(self, ciphertext: str) -> str:
        encoded = b64d(ciphertext)
        if len(encoded) < 8:
            raise ValueError
        try:
            outer = _fields(encoded)
        except ValueError:
            outer = _fields(encoded[:-8])
        is_prekey = 0x1A in outer and 0x12 in outer
        message = bytes(outer[0x22]) if is_prekey else encoded
        inner = _fields(message[:-8]) if is_prekey else outer
        index = int(inner[0x10])
        message_key = hmac.new(self.chain_key, b"\x01", hashlib.sha256).digest()
        self.chain_key = hmac.new(self.chain_key, b"\x02", hashlib.sha256).digest()
        aes_key, mac_key, iv = olm_keys(message_key)
        if not hmac.compare_digest(
            message[-8:], hmac.new(mac_key, message[:-8], hashlib.sha256).digest()[:8]
        ):
            msg = "Olm message authentication failed"
            raise ValueError(msg)
        plaintext = aes_cbc_decrypt(aes_key, iv, bytes(inner[0x22]))
        self.message_count = max(self.message_count, index + 1)
        return plaintext.decode()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "key_material": b64e(self.key_material),
            "sender_key": self.sender_key,
            "recipient_key": self.recipient_key,
            "identity_key": self.identity_key,
            "base_key": self.base_key,
            "chain_key": b64e(self.chain_key),
            "ratchet_key": self.ratchet_key,
            "message_count": self.message_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PureOlmSession:
        return cls(
            session_id=str(value["session_id"]),
            key_material=b64d(str(value["key_material"])),
            sender_key=str(value["sender_key"]),
            recipient_key=str(value["recipient_key"]),
            identity_key=str(value.get("identity_key", "")),
            base_key=str(value.get("base_key", "")),
            chain_key=b64d(str(value.get("chain_key", ""))),
            ratchet_key=str(value.get("ratchet_key", "")),
            message_count=int(value.get("message_count", 0)),
        )


class OlmSessionManager:
    def __init__(self, store: CryptoStore, account_mgr: OlmAccountManager) -> None:
        self._store, self._account_mgr = store, account_mgr
        self._sessions: dict[str, PureOlmSession] = {}

    def load(self) -> None:
        self._sessions = {}
        for session_id, value in self._store.load_sessions().items():
            if isinstance(value, dict):
                try:
                    self._sessions[str(session_id)] = PureOlmSession.from_dict(value)
                except (KeyError, TypeError, ValueError):
                    continue

    def _save(self) -> None:
        self._store.save_sessions(
            {key: value.to_dict() for key, value in self._sessions.items()}
        )

    def create_outbound_session(
        self, their_identity_key: str, their_one_time_key: str
    ) -> PureOlmSession | None:
        try:
            session = PureOlmSession.outbound(
                self._account_mgr.account, their_identity_key, their_one_time_key
            )
        except (TypeError, ValueError):
            return None
        self._sessions[session.id] = session
        self._save()
        return session

    def create_inbound_session(self, message: PureOlmMessage) -> PureOlmSession | None:
        try:
            fields = _fields(b64d(message.ciphertext))
            inner = _fields(bytes(fields[0x22])[:-8])
            payload = {
                "identity_key": b64e(bytes(fields[0x1A])),
                "one_time_key": b64e(bytes(fields[0x0A])),
                "base_key": b64e(bytes(fields[0x12])),
                "message": b64e(bytes(fields[0x22])),
                "session_id": hashlib.sha256(
                    bytes(fields[0x1A]) + bytes(inner[0x0A])
                ).hexdigest()[:32],
            }
            session = PureOlmSession.inbound(self._account_mgr.account, payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        self._sessions[session.id] = session
        self._save()
        return session

    def decrypt_to_device_message(
        self, ciphertext_body: str, message_type: int, sender_key: str
    ) -> str | None:
        if message_type != 0:
            sessions = [
                item
                for item in self._sessions.values()
                if item.sender_key == sender_key
            ]
            errors: list[str] = []
            for session in sessions:
                try:
                    plaintext = session.decrypt(ciphertext_body)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    errors.append(f"{session.id}: {type(error).__name__}: {error}")
                    continue
                self._save()
                return plaintext
            log(
                "WARNING",
                f"Olm normal message failed: sender_key={sender_key}, sessions={len(sessions)}, errors={errors}",
            )
            return None
        try:
            fields = _fields(b64d(ciphertext_body))
            inner = _fields(bytes(fields[0x22])[:-8])
            session_id = hashlib.sha256(
                bytes(fields[0x1A]) + bytes(inner[0x0A])
            ).hexdigest()[:32]
        except (KeyError, TypeError, ValueError) as error:
            log(
                "WARNING",
                f"Olm pre-key message parse failed: {type(error).__name__}: {error}",
            )
            return None
        # Match the session tuple as well as the local persistence id.
        session = self._sessions.get(session_id)
        if session is not None:
            if (
                session.sender_key != sender_key
                or session.recipient_key != b64e(bytes(fields[0x0A]))
                or session.base_key != b64e(bytes(fields[0x12]))
            ):
                session = None
        if session is None and message_type == 0:
            session = self.create_inbound_session(
                PureOlmMessage(ciphertext_body, message_type)
            )
        if session is None:
            log(
                "WARNING",
                f"Olm pre-key session creation failed: session_id={session_id}, sender_key={sender_key}",
            )
            return None
        try:
            plaintext = session.decrypt(ciphertext_body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            # A pre-key message can arrive after state was migrated from the
            # former non-standard implementation. Rebuild that session once;
            # this also handles a partially persisted session from an aborted
            # first decrypt without consuming another one-time key.
            if message_type == 0:
                self._sessions.pop(session_id, None)
                session = self.create_inbound_session(
                    PureOlmMessage(ciphertext_body, message_type)
                )
                if session is not None:
                    try:
                        plaintext = session.decrypt(ciphertext_body)
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as retry_error:
                        log(
                            "WARNING",
                            f"Olm pre-key retry failed: session_id={session_id}, "
                            f"{type(retry_error).__name__}: {retry_error}",
                        )
                        plaintext = None
                    if plaintext is not None:
                        self._save()
                        return plaintext
            log(
                "WARNING",
                f"Olm pre-key decrypt failed: session_id={session_id}, {type(error).__name__}: {error}",
            )
            return None
        self._save()
        return plaintext

    @staticmethod
    def encrypt(session: PureOlmSession, plaintext: str) -> PureOlmMessage:
        return session.encrypt(plaintext)

    @staticmethod
    def decrypt(session: PureOlmSession, message: PureOlmMessage) -> str | None:
        try:
            return session.decrypt(message.ciphertext)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            log("WARNING", "Olm message authentication failed")
            return None
