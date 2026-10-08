"""Matrix Secret Storage (SSSS) primitives.

This module intentionally contains no client or store code.  It implements the
wire algorithms used by ``m.secret_storage.v1.aes-hmac-sha2`` and the standard
passphrase key derivation so the recovery path can be tested with published
Matrix vectors independently of a homeserver.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .primitives import b64d


def _decode(value: str) -> bytes:
    return b64d(value)


def derive_passphrase_key(
    passphrase: str,
    *,
    salt: str | bytes,
    iterations: int = 500_000,
    bits: int = 256,
) -> bytes:
    """Derive an SSSS key from ``m.secret_storage.v1.pbkdf2`` parameters."""

    salt_bytes = salt.encode() if isinstance(salt, str) else salt
    if iterations <= 0 or bits not in {128, 192, 256, 512}:
        raise ValueError("invalid Secret Storage PBKDF2 parameters")
    return hashlib.pbkdf2_hmac(
        "sha512", passphrase.encode("utf-8"), salt_bytes, iterations, bits // 8
    )


def _key_parts(key: bytes) -> tuple[bytes, bytes]:
    # The SSSS key is 32 bytes.  Deriving two independent keys prevents the
    # AES and MAC keys from being reused while retaining compatibility with
    # clients that pass a 64-byte expanded key.
    if len(key) == 64:
        return key[:32], key[32:]
    if len(key) != 32:
        raise ValueError("Secret Storage key must be 32 bytes")
    expanded = HKDF(algorithm=SHA256(), length=64, salt=b"", info=b"").derive(key)
    return expanded[:32], expanded[32:]


def decrypt_secret(key: bytes, encrypted: dict[str, Any]) -> bytes:
    """Decrypt and authenticate one encrypted account-data secret."""

    try:
        iv = _decode(str(encrypted["iv"]))
        ciphertext = _decode(str(encrypted["ciphertext"]))
        actual_mac = _decode(str(encrypted["mac"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Secret Storage encrypted payload") from exc
    if len(iv) != 16:
        raise ValueError("Secret Storage IV must be 16 bytes")
    aes_key, mac_key = _key_parts(key)
    expected_mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    # A few early clients encoded the MAC over the base64 text.  Accepting it
    # only as a fallback keeps old backups readable without weakening the
    # normal binary MAC check.
    if not hmac.compare_digest(actual_mac, expected_mac):
        encoded_mac = hmac.new(
            mac_key, str(encrypted["ciphertext"]).encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(actual_mac, encoded_mac):
            raise ValueError("Secret Storage MAC verification failed")
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def decrypt_secret_json(key: bytes, encrypted: dict[str, Any]) -> Any:
    import json

    try:
        return json.loads(decrypt_secret(key, encrypted).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Secret Storage secret is not valid JSON") from exc


def decode_recovery_key(value: str) -> bytes:
    """Decode a Matrix recovery key, preserving compatibility with raw keys."""

    import base58

    text = "".join(
        line.strip()
        for line in value.strip().splitlines()
        if line.strip() and not line.strip().startswith("-----")
    )
    try:
        decoded = base58.b58decode(text)
    except Exception as exc:
        raise ValueError("invalid Matrix recovery key encoding") from exc
    # Matrix recovery keys use 0x8b and a version byte before the 32-byte
    # Curve25519 secret.  Some clients expose the raw 32-byte value instead.
    if len(decoded) >= 34 and decoded[:2] == b"\x8b\x01":
        decoded = decoded[2:]
    elif len(decoded) >= 33 and decoded[0] == 0x8B:
        decoded = decoded[1:]
    if len(decoded) > 32:
        decoded = decoded[:32]
    if len(decoded) != 32:
        raise ValueError("Matrix recovery key must contain a 32-byte private key")
    return decoded


__all__ = (
    "decode_recovery_key",
    "decrypt_secret",
    "decrypt_secret_json",
    "derive_passphrase_key",
)
