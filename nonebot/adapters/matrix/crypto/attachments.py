"""Matrix encrypted attachment v2 and password-protected room key exports."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from .primitives import b64d, b64e
from .secret_storage import crypt_ctr


def encrypt_attachment(data: bytes) -> tuple[bytes, dict[str, Any]]:
    key, iv = os.urandom(32), os.urandom(8) + bytes(8)
    encrypted = crypt_ctr(key, iv, data)
    return encrypted, {
        "v": "v2",
        "key": {
            "kty": "oct",
            "alg": "A256CTR",
            "ext": True,
            "key_ops": ["encrypt", "decrypt"],
            "k": b64e(key).replace("+", "-").replace("/", "_"),
        },
        "iv": b64e(iv),
        "hashes": {"sha256": b64e(hashlib.sha256(encrypted).digest())},
    }


def decrypt_attachment(data: bytes, descriptor: dict[str, Any]) -> bytes:
    key = descriptor["key"]
    if (
        descriptor.get("v") != "v2"
        or key.get("kty") != "oct"
        or key.get("alg") != "A256CTR"
    ):
        raise ValueError("Unsupported encrypted attachment")
    if not hmac.compare_digest(
        hashlib.sha256(data).digest(), b64d(descriptor["hashes"]["sha256"])
    ):
        raise ValueError("Attachment digest mismatch")
    raw_key, iv = b64d(key["k"]), b64d(descriptor["iv"])
    if len(raw_key) != 32 or len(iv) != 16:
        raise ValueError("Invalid encrypted attachment key or IV")
    return crypt_ctr(raw_key, iv, data)


def export_room_keys(
    keys: list[dict[str, Any]], passphrase: str, rounds: int = 500000
) -> str:
    salt, iv = os.urandom(16), bytearray(os.urandom(16))
    iv[8] &= 0x7F
    derived = hashlib.pbkdf2_hmac("sha512", passphrase.encode(), salt, rounds, 64)
    encrypted = crypt_ctr(
        derived[:32], bytes(iv), json.dumps(keys, separators=(",", ":")).encode()
    )
    payload = b"\x01" + salt + bytes(iv) + rounds.to_bytes(4, "big") + encrypted
    encoded = b64e(payload + hmac.digest(derived[32:], payload, "sha256"))
    return f"-----BEGIN MEGOLM SESSION DATA-----\n{encoded}\n-----END MEGOLM SESSION DATA-----"


def import_room_keys(text: str, passphrase: str) -> list[dict[str, Any]]:
    lines = text.strip().splitlines()
    if (
        lines[0] != "-----BEGIN MEGOLM SESSION DATA-----"
        or lines[-1] != "-----END MEGOLM SESSION DATA-----"
    ):
        raise ValueError("Invalid room key export header")
    data = b64d("".join(lines[1:-1]))
    if len(data) < 69 or data[0] != 1:
        raise ValueError("Unsupported room key export")
    rounds = int.from_bytes(data[33:37], "big")
    if not 1 <= rounds <= 10_000_000:
        raise ValueError("Invalid room key export KDF rounds")
    derived = hashlib.pbkdf2_hmac("sha512", passphrase.encode(), data[1:17], rounds, 64)
    if not hmac.compare_digest(
        hmac.digest(derived[32:], data[:-32], "sha256"), data[-32:]
    ):
        raise ValueError("Room key export MAC verification failed")
    value = json.loads(crypt_ctr(derived[:32], data[17:33], data[37:-32]))
    if not isinstance(value, list):
        raise ValueError("Room key export is not an array")
    return value
