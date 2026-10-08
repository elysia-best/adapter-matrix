"""Small, serialisable cryptographic building blocks used by the adapter.

The public crypto engine deliberately depends on these data-only classes instead of
on a particular native binding.  The wire formats used here are stable JSON/base64
records, which also makes the state machine easy to persist and test.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.padding import PKCS7


def b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii").rstrip("=")


def b64d(value: str) -> bytes:
    value = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def hkdf(key_material: bytes, *, info: bytes, length: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info,
    ).derive(key_material)


def aes_ctr_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    return cipher.encryptor().update(plaintext)


def aes_ctr_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    return cipher.decryptor().update(ciphertext)


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    padded = PKCS7(algorithms.AES.block_size).padder()
    value = padded.update(plaintext) + padded.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(value) + encryptor.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadded = PKCS7(algorithms.AES.block_size).unpadder()
    return unpadded.update(padded) + unpadded.finalize()


def olm_keys(message_key: bytes) -> tuple[bytes, bytes, bytes]:
    derived = hkdf(message_key, info=b"OLM_KEYS", length=80)
    return derived[:32], derived[32:64], derived[64:]


def megolm_keys(ratchet: bytes) -> tuple[bytes, bytes, bytes]:
    derived = hkdf(ratchet, info=b"MEGOLM_KEYS", length=80)
    return derived[:32], derived[32:64], derived[64:]


@dataclass(slots=True)
class IdentityAccount:
    """An Ed25519/X25519 identity and its one-time keys."""

    ed25519_private: bytes
    curve25519_private: bytes
    one_time_private: dict[str, bytes]
    fallback_private: dict[str, bytes]
    next_key_id: int = 0

    @classmethod
    def create(cls) -> IdentityAccount:
        return cls(
            ed25519_private=Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
            curve25519_private=X25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
            one_time_private={},
            fallback_private={},
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> IdentityAccount:
        return cls(
            ed25519_private=b64d(str(value["ed25519_private"])),
            curve25519_private=b64d(str(value["curve25519_private"])),
            one_time_private={
                str(k): b64d(str(v))
                for k, v in dict(value.get("one_time_private", {})).items()
            },
            fallback_private={
                str(k): b64d(str(v))
                for k, v in dict(value.get("fallback_private", {})).items()
            },
            next_key_id=int(value.get("next_key_id", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "ed25519_private": b64e(self.ed25519_private),
            "curve25519_private": b64e(self.curve25519_private),
            "one_time_private": {
                key_id: b64e(value) for key_id, value in self.one_time_private.items()
            },
            "fallback_private": {
                key_id: b64e(value) for key_id, value in self.fallback_private.items()
            },
            "next_key_id": self.next_key_id,
        }

    @property
    def identity_keys(self) -> dict[str, str]:
        ed = Ed25519PrivateKey.from_private_bytes(self.ed25519_private)
        curve = X25519PrivateKey.from_private_bytes(self.curve25519_private)
        return {
            "ed25519": b64e(
                ed.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            ),
            "curve25519": b64e(
                curve.public_key().public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            ),
        }

    @property
    def one_time_keys(self) -> dict[str, dict[str, str]]:
        return {
            "curve25519": {
                key_id: b64e(
                    X25519PrivateKey.from_private_bytes(private)
                    .public_key()
                    .public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw,
                    )
                )
                for key_id, private in self.one_time_private.items()
            }
        }

    @property
    def fallback_key(self) -> dict[str, dict[str, str]]:
        return {
            "curve25519": {
                key_id: b64e(
                    X25519PrivateKey.from_private_bytes(private)
                    .public_key()
                    .public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw,
                    )
                )
                for key_id, private in self.fallback_private.items()
            }
        }

    @property
    def max_one_time_keys(self) -> int:
        return 100

    def generate_one_time_keys(self, count: int) -> None:
        for _ in range(max(0, count)):
            key_id = str(self.next_key_id)
            self.next_key_id += 1
            self.one_time_private[key_id] = X25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )

    def generate_fallback_key(self) -> None:
        if self.fallback_private:
            return
        self.fallback_private = {}
        key_id = str(self.next_key_id)
        self.next_key_id += 1
        self.fallback_private[key_id] = X25519PrivateKey.generate().private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    def mark_keys_as_published(self) -> None:
        return

    def sign(self, message: bytes) -> str:
        return b64e(
            Ed25519PrivateKey.from_private_bytes(self.ed25519_private).sign(message)
        )

    def private_for_one_time_key(self, public_key: bytes) -> tuple[str, bytes] | None:
        for key_id, private in {
            **self.one_time_private,
            **self.fallback_private,
        }.items():
            candidate = (
                X25519PrivateKey.from_private_bytes(private)
                .public_key()
                .public_bytes(
                    serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw,
                )
            )
            if hmac.compare_digest(candidate, public_key):
                return key_id, private
        return None


def verify_device_signature(
    *, user_id: str, device_keys: dict[str, Any], canonical_payload: bytes
) -> bool:
    signatures = device_keys.get("signatures", {})
    user_signatures = (
        signatures.get(user_id, {}) if isinstance(signatures, dict) else {}
    )
    key_id = next((key for key in user_signatures if key.startswith("ed25519:")), None)
    public = next(
        (
            value
            for key, value in device_keys.get("keys", {}).items()
            if key.startswith("ed25519:")
        ),
        None,
    )
    signature = user_signatures.get(key_id) if key_id else None
    if not isinstance(public, str) or not isinstance(signature, str):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public)).verify(
            b64d(signature), canonical_payload
        )
    except (ValueError, TypeError):
        return False
    return True


def derive_shared_key(private: bytes, public: str, *, info: bytes) -> bytes:
    shared = X25519PrivateKey.from_private_bytes(private).exchange(
        X25519PublicKey.from_public_bytes(b64d(public))
    )
    return hkdf(shared, info=info, length=64)


def encrypt_record(key_material: bytes, plaintext: bytes, *, context: bytes) -> str:
    nonce = os.urandom(16)
    key = hkdf(key_material, info=context + b" aes", length=32)
    mac_key = hkdf(key_material, info=context + b" mac", length=32)
    ciphertext = aes_ctr_encrypt(key, nonce, plaintext)
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return b64e(
        canonical_json(
            {"nonce": b64e(nonce), "ciphertext": b64e(ciphertext), "mac": b64e(mac)}
        )
    )


def decrypt_record(key_material: bytes, record: str, *, context: bytes) -> bytes:
    try:
        payload = json.loads(b64d(record))
    except (ValueError, json.JSONDecodeError) as exc:
        msg = "encrypted record is not valid base64 JSON"
        raise ValueError(msg) from exc
    nonce = b64d(str(payload["nonce"]))
    ciphertext = b64d(str(payload["ciphertext"]))
    mac = b64d(str(payload["mac"]))
    mac_key = hkdf(key_material, info=context + b" mac", length=32)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        msg = "encrypted record authentication failed"
        raise ValueError(msg)
    key = hkdf(key_material, info=context + b" aes", length=32)
    return aes_ctr_decrypt(key, nonce, ciphertext)
