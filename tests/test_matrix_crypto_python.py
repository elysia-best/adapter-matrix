from __future__ import annotations

from pathlib import Path

from nonebot.adapters.matrix.crypto.account import OlmAccountManager
from nonebot.adapters.matrix.crypto.attachments import (
    decrypt_attachment,
    encrypt_attachment,
    export_room_keys,
    import_room_keys,
)
from nonebot.adapters.matrix.crypto.megolm import MegolmManager
from nonebot.adapters.matrix.crypto.native import pk_from_private
from nonebot.adapters.matrix.crypto.primitives import MEGOLM_ALGORITHM, canonical_json
from nonebot.adapters.matrix.crypto.secret_storage import (
    decode_recovery_key,
    decrypt_secret,
    encode_recovery_key,
    encrypt_secret,
)
from nonebot.adapters.matrix.crypto.sessions import OlmSessionManager
from nonebot.adapters.matrix.crypto.store import CryptoStore
from nonebot.adapters.matrix.crypto.types import DecryptionError, StoreError, TaskLock

import olm
import pytest


def test_olm_bidirectional_session_and_pickle_restart(tmp_path: Path) -> None:
    stores = [
        CryptoStore(tmp_path / name, ("hs", f"@{name}:hs", name)) for name in ("A", "B")
    ]
    accounts = [OlmAccountManager(store) for store in stores]
    for account in accounts:
        account.load_or_create()
    _, receiver = accounts
    receiver.account.generate_one_time_keys(1)
    receiver.save()
    one_time_key = next(iter(receiver.account.one_time_keys["curve25519"].values()))
    sessions, inbound_sessions = [
        OlmSessionManager(store, account)
        for store, account in zip(stores, accounts, strict=True)
    ]
    sender_key, receiver_key = [
        account.account.identity_keys["curve25519"] for account in accounts
    ]
    outbound = sessions.create_outbound_session(receiver_key, one_time_key)
    first = sessions.encrypt(outbound, receiver_key, "hello")
    assert (
        inbound_sessions.decrypt_to_device_message(
            first["body"], first["type"], sender_key
        )
        == "hello"
    )
    inbound = inbound_sessions.get(sender_key)
    assert inbound is not None
    reply = inbound_sessions.encrypt(inbound, sender_key, "reply")
    assert (
        sessions.decrypt_to_device_message(reply["body"], reply["type"], receiver_key)
        == "reply"
    )
    identities = [account.account.identity_keys for account in accounts]
    for store in stores:
        store.close()
    reopened = CryptoStore(tmp_path / "A", ("hs", "@A:hs", "A"))
    account = OlmAccountManager(reopened)
    account.load_or_create()
    assert account.account.identity_keys == identities[0]
    restored = OlmSessionManager(reopened, account)
    session = restored.get(receiver_key)
    assert session is not None
    follow_up = restored.encrypt(session, receiver_key, "again")
    assert follow_up["type"] == 1
    reopened_receiver = CryptoStore(tmp_path / "B", ("hs", "@B:hs", "B"))
    account_b = OlmAccountManager(reopened_receiver)
    account_b.load_or_create()
    assert (
        OlmSessionManager(reopened_receiver, account_b).decrypt_to_device_message(
            follow_up["body"], follow_up["type"], sender_key
        )
        == "again"
    )
    reopened.close()
    reopened_receiver.close()


def test_megolm_origin_binding_replay_and_restart(tmp_path: Path) -> None:
    store = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    manager = MegolmManager(store)
    outbound = olm.OutboundGroupSession()
    value = {
        "algorithm": MEGOLM_ALGORITHM,
        "room_id": "!room:hs",
        "session_id": outbound.id,
        "session_key": outbound.session_key,
        "sender_key": "curve",
        "sender_claimed_keys": {"ed25519": "ed"},
        "sender": "@user:hs",
        "forwarding_curve25519_key_chain": [],
    }
    assert manager.import_key(value, exported=False)
    ciphertext = outbound.encrypt(
        canonical_json(
            {"room_id": "!room:hs", "type": "m.room.message", "content": {"body": "x"}}
        )
    )
    raw = {
        "event_id": "$event",
        "origin_server_ts": 1,
        "sender": "@user:hs",
        "content": {
            "algorithm": MEGOLM_ALGORITHM,
            "session_id": outbound.id,
            "sender_key": "curve",
            "ciphertext": ciphertext,
        },
    }
    assert manager.decrypt("!room:hs", raw)["content"]["body"] == "x"
    for changes, code in (
        ({"event_id": "$other"}, "ReplayedMessage"),
        ({"sender": "@evil:hs"}, "SenderMismatch"),
    ):
        with pytest.raises(DecryptionError, match=code):
            manager.decrypt("!room:hs", {**raw, **changes})
    with pytest.raises(DecryptionError, match="MissingRoomKey"):
        manager.decrypt("!wrong:hs", raw)
    store.close()
    reopened = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    assert MegolmManager(reopened).decrypt("!room:hs", raw)["content"]["body"] == "x"
    reopened.close()


def test_megolm_rotation(tmp_path: Path) -> None:
    store = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    manager = MegolmManager(store)
    account = olm.Account()
    identity = {**account.identity_keys, "user_id": "@user:hs"}
    first, record = manager.outbound("!room:hs", {"rotation_period_msgs": 2}, identity)
    for _ in range(2):
        first.encrypt("payload")
        manager.save_outbound("!room:hs", first, record)
    second, _ = manager.outbound("!room:hs", {"rotation_period_msgs": 2}, identity)
    assert second.id != first.id
    store.close()


def test_attachment_and_room_key_export_are_authenticated() -> None:
    ciphertext, descriptor = encrypt_attachment(b"attachment")
    assert decrypt_attachment(ciphertext, descriptor) == b"attachment"
    with pytest.raises(ValueError, match="digest"):
        decrypt_attachment(ciphertext + b"x", descriptor)
    exported = export_room_keys(
        [{"room_id": "!room:hs", "session_id": "s"}], "passphrase", rounds=1
    )
    assert import_room_keys(exported, "passphrase")[0]["session_id"] == "s"
    with pytest.raises(ValueError, match="MAC"):
        import_room_keys(exported, "wrong")


def test_ssss_name_binding_and_recovery_checksum() -> None:
    key = bytes(range(32))
    encrypted = encrypt_secret(key, b"secret", "m.cross_signing.master")
    assert decrypt_secret(key, encrypted, "m.cross_signing.master") == b"secret"
    with pytest.raises(ValueError, match="MAC"):
        decrypt_secret(key, encrypted, "m.cross_signing.self_signing")
    recovery = encode_recovery_key(key)
    assert decode_recovery_key(recovery) == key
    with pytest.raises(ValueError, match="checksum"):
        decode_recovery_key(recovery + " 1")


def test_pk_private_import_uses_native_authenticated_encryption() -> None:
    decryptor = pk_from_private(bytes(range(32)))
    message = olm.PkEncryption(decryptor.public_key).encrypt("backup secret")
    assert decryptor.decrypt(message) == "backup secret"
    with pytest.raises(olm.PkDecryptionError):
        decryptor.decrypt(
            olm.PkMessage(message.ephemeral_key, "A" * 11, message.ciphertext)
        )


def test_store_rejects_legacy_without_deleting_it(tmp_path: Path) -> None:
    legacy = tmp_path / "account.pickle"
    legacy.write_bytes(b"old")
    with pytest.raises(StoreError, match="Legacy"):
        CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    assert legacy.read_bytes() == b"old"


def test_store_identity_permissions_and_transaction_rollback(tmp_path: Path) -> None:
    store = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))

    def rollback() -> None:
        error = ValueError("rollback")
        with store.transaction():
            store.put("secret/test", "private")
            raise error

    with pytest.raises(ValueError, match="rollback"):
        rollback()
    assert store.get("secret/test") is None
    account = OlmAccountManager(store)
    account.load_or_create()
    store.close()
    with pytest.raises(StoreError, match="different"):
        CryptoStore(tmp_path, ("other", "@user:hs", "D"))
    assert b"private" not in (tmp_path / "crypto.sqlite3").read_bytes()
    import os

    if os.name != "nt":
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "store.key").stat().st_mode & 0o777 == 0o600


def test_missing_or_corrupt_store_never_generates_an_account(tmp_path: Path) -> None:
    store = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    store.close()
    reopened = CryptoStore(tmp_path, ("hs", "@user:hs", "D"))
    with pytest.raises(StoreError, match="Account is missing"):
        OlmAccountManager(reopened).load_or_create()
    reopened.close()
    (tmp_path / "crypto.sqlite3").write_bytes(b"damaged")
    with pytest.raises(StoreError, match="Cannot open"):
        CryptoStore(tmp_path, ("hs", "@user:hs", "D"))


async def test_task_lock_serializes_and_allows_nested_replies() -> None:
    import asyncio

    lock = TaskLock()
    order = []

    async def operation(value: int) -> None:
        async with lock:
            order.append(value)
            await asyncio.sleep(0)
            async with lock:
                order.append(value)

    await asyncio.wait_for(asyncio.gather(operation(1), operation(2)), 1)
    assert order == [1, 1, 2, 2]
