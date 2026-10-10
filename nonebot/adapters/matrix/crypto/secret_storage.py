"""Matrix Secret Storage v1 and SDK-style secret store handles."""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import base58
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .primitives import b64d, b64e
from .types import CryptoError

ALGORITHM = "m.secret_storage.v1.aes-hmac-sha2"
SECRETS = (
    "m.cross_signing.master",
    "m.cross_signing.self_signing",
    "m.cross_signing.user_signing",
    "m.megolm_backup.v1",
)


def derive_passphrase_key(
    passphrase: str, *, salt: str, iterations: int = 500000, bits: int = 256
) -> bytes:
    if not 1 <= iterations <= 10_000_000 or bits != 256:
        raise ValueError("Invalid Secret Storage PBKDF2 parameters")
    return hashlib.pbkdf2_hmac(
        "sha512", passphrase.encode(), salt.encode(), iterations, 32
    )


def _key_parts(key: bytes, name: str = "") -> tuple[bytes, bytes]:
    if len(key) != 32:
        raise ValueError("Secret Storage key must contain 32 bytes")
    material = HKDF(
        algorithm=SHA256(), length=64, salt=bytes(32), info=name.encode()
    ).derive(key)
    return material[:32], material[32:]


def crypt_ctr(key: bytes, iv: bytes, value: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return cipher.update(value) + cipher.finalize()


def encrypt_secret(
    key: bytes, plaintext: bytes, name: str = "", *, iv: bytes | None = None
) -> dict[str, str]:
    aes_key, mac_key = _key_parts(key, name)
    if iv is None:
        nonce = bytearray(os.urandom(16))
        nonce[8] &= 0x7F
        iv = bytes(nonce)
    ciphertext = crypt_ctr(aes_key, iv, plaintext)
    return {
        "iv": b64e(iv),
        "ciphertext": b64e(ciphertext),
        "mac": b64e(hmac.digest(mac_key, ciphertext, "sha256")),
    }


def decrypt_secret(key: bytes, encrypted: dict[str, Any], name: str = "") -> bytes:
    iv, ciphertext, mac = (
        b64d(encrypted[field]) for field in ("iv", "ciphertext", "mac")
    )
    if len(iv) != 16 or len(mac) != 32:
        raise ValueError("Invalid Secret Storage payload")
    aes_key, mac_key = _key_parts(key, name)
    if not hmac.compare_digest(mac, hmac.digest(mac_key, ciphertext, "sha256")):
        raise ValueError("Secret Storage MAC verification failed")
    return crypt_ctr(aes_key, iv, ciphertext)


def encode_recovery_key(key: bytes) -> str:
    if len(key) != 32:
        raise ValueError("Recovery key must contain 32 bytes")
    value = b"\x8b\x01" + key
    parity = 0
    for byte in value:
        parity ^= byte
    text = base58.b58encode(value + bytes([parity])).decode()
    return " ".join(text[i : i + 4] for i in range(0, len(text), 4))


def decode_recovery_key(value: str) -> bytes:
    data = base58.b58decode("".join(value.split()))
    parity = 0
    for byte in data:
        parity ^= byte
    if len(data) != 35 or data[:2] != b"\x8b\x01" or parity != 0:
        raise ValueError("Invalid Matrix recovery key or checksum")
    return data[2:-1]


class SecretStore:
    def __init__(self, engine: Any, key_id: str, key: bytes) -> None:
        self.engine, self.key_id, self._key = engine, key_id, key

    def secret_storage_key(self) -> str:
        return encode_recovery_key(self._key)

    async def get_secret(self, secret_name: str) -> str | None:
        data = await self.engine.account_data(secret_name)
        if data is None:
            return None
        value = data.get("encrypted", {}).get(self.key_id)
        if value is None:
            raise CryptoError("Secret is not encrypted by the selected storage key")
        return decrypt_secret(self._key, value, secret_name).decode("utf-8")

    async def put_secret(self, secret_name: str, secret: str) -> None:
        data = await self.engine.account_data(secret_name) or {}
        encrypted = data.get("encrypted", {})
        encrypted[self.key_id] = encrypt_secret(self._key, secret.encode(), secret_name)
        await self.engine.call(
            "set_account_data", event_type=secret_name, content={"encrypted": encrypted}
        )

    async def import_secrets(self) -> None:
        for name in SECRETS:
            secret = await self.get_secret(name)
            if secret is not None:
                await self.engine.import_secret(name, secret)
        if self.engine.cross_signing.status()["has_self_signing"]:
            from .identities import Device

            await self.engine.cross_signing.sign_device(
                Device(self.engine, self.engine.user_id, self.engine.device_id)
            )

    async def export_secrets(self) -> None:
        for name in SECRETS:
            secret = self.engine.store.get(f"secret/{name}")
            if secret is not None:
                await self.put_secret(name, secret)


class SecretStorage:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def fetch_default_key_id(self) -> str | None:
        value = await self.engine.account_data("m.secret_storage.default_key")
        return value.get("key") if value else None

    async def is_enabled(self) -> bool:
        return await self.fetch_default_key_id() is not None

    async def open_secret_store(self, secret_storage_key: str) -> SecretStore:
        key_id = await self.fetch_default_key_id()
        if not key_id:
            raise CryptoError("Secret Storage is not enabled")
        info = await self.engine.account_data(f"m.secret_storage.key.{key_id}")
        if not info or info.get("algorithm") != ALGORITHM:
            raise CryptoError("Unsupported Secret Storage key")
        try:
            key = decode_recovery_key(secret_storage_key)
        except ValueError:
            parameters = info.get("passphrase", {})
            if parameters.get("algorithm") != "m.pbkdf2":
                raise
            key = derive_passphrase_key(
                secret_storage_key,
                salt=parameters["salt"],
                iterations=parameters["iterations"],
                bits=parameters.get("bits", 256),
            )
        if "iv" in info or "mac" in info:
            check = encrypt_secret(key, bytes(32), iv=b64d(info["iv"]))
            if not hmac.compare_digest(b64d(check["mac"]), b64d(info["mac"])):
                raise ValueError("Secret Storage key check failed")
        return SecretStore(self.engine, key_id, key)

    async def create_secret_store(
        self, *, passphrase: str | None = None
    ) -> SecretStore:
        key_id = b64e(os.urandom(24))
        info: dict[str, Any] = {"algorithm": ALGORITHM}
        if passphrase is None:
            key = os.urandom(32)
        else:
            parameters = {
                "algorithm": "m.pbkdf2",
                "salt": b64e(os.urandom(24)),
                "iterations": 500000,
                "bits": 256,
            }
            key = derive_passphrase_key(passphrase, salt=parameters["salt"])
            info["passphrase"] = parameters
        check = encrypt_secret(key, bytes(32))
        info.update(iv=check["iv"], mac=check["mac"])
        # Persist before network writes, so interruption cannot lose the new key.
        self.engine.store.put(
            "ssss_pending", {"key_id": key_id, "key": b64e(key), "info": info}
        )
        await self.engine.call(
            "set_account_data",
            event_type=f"m.secret_storage.key.{key_id}",
            content=info,
        )
        store = SecretStore(self.engine, key_id, key)
        await store.export_secrets()
        await self.engine.call(
            "set_account_data",
            event_type="m.secret_storage.default_key",
            content={"key": key_id},
        )
        self.engine.store.put("ssss_key", {"key_id": key_id, "key": b64e(key)})
        self.engine.store.delete("ssss_pending")
        return store
