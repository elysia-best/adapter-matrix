from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from nonebot.adapters.matrix.crypto.account import OlmAccountManager
from nonebot.adapters.matrix.crypto.megolm import GroupSession
from nonebot.adapters.matrix.crypto.primitives import IdentityAccount
from nonebot.adapters.matrix.crypto.secret_storage import (
    _key_parts,
    decrypt_secret,
    derive_passphrase_key,
)
from nonebot.adapters.matrix.crypto.sessions import OlmSessionManager, PureOlmSession
from nonebot.adapters.matrix.crypto.store import CryptoStore

import pytest


def test_python_olm_record_roundtrip_and_tamper_detection() -> None:
    sender = IdentityAccount.create()
    recipient = IdentityAccount.create()
    recipient.generate_one_time_keys(1)
    one_time_key = next(iter(recipient.one_time_keys["curve25519"].values()))

    outbound = PureOlmSession.outbound(
        sender, recipient.identity_keys["curve25519"], one_time_key
    )
    message = outbound.encrypt("hello")
    with TemporaryDirectory() as directory:
        store = CryptoStore(Path(directory))
        account = OlmAccountManager(store)
        account._account = recipient
        sessions = OlmSessionManager(store, account)
        inbound = sessions.create_inbound_session(message)
        assert inbound is not None
        assert inbound.decrypt(message.ciphertext) == "hello"
        follow_up = outbound.encrypt("follow-up")
        assert follow_up.message_type == 1
        assert inbound.decrypt(follow_up.ciphertext) == "follow-up"

    payload = bytearray(base64.b64decode(message.ciphertext + "=="))
    payload[-1] ^= 1
    tampered = base64.b64encode(payload).decode().rstrip("=")
    with pytest.raises(ValueError, match=r"authentication|base64|padding"):
        inbound.decrypt(tampered)


def test_python_megolm_roundtrip_and_json_persistence() -> None:
    session = GroupSession.create()
    encrypted = session.encrypt('{"type":"m.room.message"}')
    restored = GroupSession(session.session_id, session.session_key)
    plaintext, index = restored.decrypt(encrypted)
    assert plaintext == '{"type":"m.room.message"}'
    assert index == 0

    account = IdentityAccount.create()
    with TemporaryDirectory() as directory:
        store = CryptoStore(Path(directory))
        store.save_account(account)
        loaded = store.load_account()
        assert loaded is not None
        assert loaded.identity_keys == account.identity_keys
        assert (Path(directory) / "account.json").exists()
        assert not (Path(directory) / "account.pickle").exists()


def test_fallback_key_is_stable_until_explicitly_removed() -> None:
    account = IdentityAccount.create()
    account.generate_fallback_key()
    first = account.fallback_key
    account.generate_fallback_key()
    assert account.fallback_key == first


def test_python_olm_session_manager_persists_json_state() -> None:
    with TemporaryDirectory() as directory:
        store = CryptoStore(Path(directory))
        account = OlmAccountManager(store)
        account.load_or_create()
        peer = IdentityAccount.create()
        peer.generate_one_time_keys(1)
        peer_key = next(iter(peer.one_time_keys["curve25519"].values()))

        sessions = OlmSessionManager(store, account)
        session = sessions.create_outbound_session(
            peer.identity_keys["curve25519"], peer_key
        )
        assert session is not None

        restored = OlmSessionManager(store, account)
        restored.load()
        assert session.id in restored._sessions
        assert restored._sessions[session.id].key_material == session.key_material


def test_secret_storage_passphrase_decrypts_authenticated_secret() -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = derive_passphrase_key("test passphrase", salt="salt", iterations=1)
    aes_key, mac_key = _key_parts(key)
    iv = os.urandom(16)
    plaintext = b'{"private_key":"secret"}'
    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    def encoded(value: bytes) -> str:
        return base64.b64encode(value).decode().rstrip("=")
    encrypted = {
        "iv": encoded(iv),
        "ciphertext": encoded(ciphertext),
        "mac": encoded(hmac.new(mac_key, ciphertext, hashlib.sha256).digest()),
    }
    assert decrypt_secret(key, encrypted) == plaintext


def test_legacy_pickle_store_is_rejected_without_deletion(tmp_path: Path) -> None:
    legacy = tmp_path / "account.pickle"
    legacy.write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="legacy non-standard"):
        CryptoStore(tmp_path)
    assert legacy.exists()
