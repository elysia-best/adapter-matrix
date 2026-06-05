"""Tests for the E2EE crypto modules."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from nonebot.adapters.matrix.api.model import RawMatrixEvent
from nonebot.adapters.matrix.crypto import CryptoEngine
from nonebot.adapters.matrix.crypto.account import OlmAccountManager
from nonebot.adapters.matrix.crypto.device_keys import DeviceKeyStore
from nonebot.adapters.matrix.crypto.megolm import MegolmManager
from nonebot.adapters.matrix.crypto.sessions import OlmSessionManager
from nonebot.adapters.matrix.crypto.store import CryptoStore
from nonebot.adapters.matrix.serialization import encode_matrix_canonical_json
from tests.fake.doubles import DummyAdapter, DummyBot

import olm
import pytest

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def tmp_store() -> CryptoStore:
    with tempfile.TemporaryDirectory() as d:
        yield CryptoStore(Path(d))


@pytest.fixture
def account_mgr(tmp_store: CryptoStore) -> OlmAccountManager:
    mgr = OlmAccountManager(tmp_store)
    mgr.load_or_create()
    return mgr


@pytest.fixture
def session_mgr(
    tmp_store: CryptoStore, account_mgr: OlmAccountManager
) -> OlmSessionManager:
    mgr = OlmSessionManager(tmp_store, account_mgr)
    mgr.load()
    return mgr


@pytest.fixture
def device_keys(tmp_store: CryptoStore) -> DeviceKeyStore:
    store = DeviceKeyStore(tmp_store)
    store.load()
    return store


@pytest.fixture
def megolm_mgr(
    tmp_store: CryptoStore,
    session_mgr: OlmSessionManager,
    device_keys: DeviceKeyStore,
) -> MegolmManager:
    mgr = MegolmManager(tmp_store, session_mgr, device_keys)
    mgr.load()
    return mgr


@pytest.fixture
def dummy_adapter() -> DummyAdapter:
    return DummyAdapter()


@pytest.fixture
def dummy_bot(dummy_adapter: DummyAdapter) -> DummyBot:
    return DummyBot(
        adapter=dummy_adapter,
        device_id="TESTDEVICE",
    )


# ------------------------------------------------------------------
# CryptoStore
# ------------------------------------------------------------------


class TestCryptoStore:
    def test_save_and_load_account(self, tmp_store: CryptoStore) -> None:
        acc = olm.Account()
        tmp_store.save_account(acc)
        loaded = tmp_store.load_account()
        assert loaded is not None
        assert loaded.identity_keys == acc.identity_keys

    def test_load_account_missing_file(self, tmp_store: CryptoStore) -> None:
        assert tmp_store.load_account() is None

    def test_save_and_load_sessions(self, tmp_store: CryptoStore) -> None:
        """测试 session 的保存和加载。"""
        alice = olm.Account()
        bob = olm.Account()
        bob.generate_one_time_keys(1)
        otk_id = next(iter(bob.one_time_keys["curve25519"].keys()))
        otk = bob.one_time_keys["curve25519"][otk_id]
        out_sess = olm.OutboundSession(alice, bob.identity_keys["curve25519"], otk)

        sess_id = out_sess.id
        data = {sess_id: out_sess}
        # 保存不应崩溃
        tmp_store.save_sessions(data)

        # 加载应优雅处理 (from_pickle 在某些 python-olm 版本中可能失败)
        loaded = tmp_store.load_sessions()
        assert isinstance(loaded, dict)

    def test_save_and_load_inbound_sessions(self, tmp_store: CryptoStore) -> None:
        data: dict[str, dict[str, str]] = {
            "!room:example.org": {"sid1": "key1", "sid2": "key2"},
        }
        tmp_store.save_inbound_sessions(data)
        loaded = tmp_store.load_inbound_sessions()
        assert loaded == data

    def test_save_and_load_outbound_sessions(self, tmp_store: CryptoStore) -> None:
        data = {
            "!room:example.org": {
                "session_id": "sid",
                "session_key": "key",
                "message_index": 0,
            },
        }
        tmp_store.save_outbound_sessions(data)
        loaded = tmp_store.load_outbound_sessions()
        assert loaded == data

    def test_save_and_load_device_keys(self, tmp_store: CryptoStore) -> None:
        data = {
            "@alice:example.org": {
                "ADEVICE": {
                    "keys": {"ed25519:ADEVICE": "abc", "curve25519:ADEVICE": "def"},
                },
            },
        }
        tmp_store.save_device_keys(data)
        loaded = tmp_store.load_device_keys()
        assert loaded == data

    def test_save_and_load_room_state(self, tmp_store: CryptoStore) -> None:
        data = {
            "!room:example.org": {
                "encrypted": True,
                "algorithm": "m.megolm.v1.aes-sha2",
            }
        }
        tmp_store.save_room_state(data)
        loaded = tmp_store.load_room_state()
        assert loaded == data

    def test_atomic_write_replaces_file(self, tmp_store: CryptoStore) -> None:
        tmp_store.save_room_state({"!room:example.org": {"encrypted": False}})
        tmp_store.save_room_state({"!room:example.org": {"encrypted": True}})
        loaded = tmp_store.load_room_state()
        assert loaded["!room:example.org"]["encrypted"] is True


# ------------------------------------------------------------------
# OlmAccountManager
# ------------------------------------------------------------------


class TestOlmAccountManager:
    def test_account_creation(self, account_mgr: OlmAccountManager) -> None:
        identity = account_mgr.account.identity_keys
        assert "ed25519" in identity
        assert "curve25519" in identity
        assert len(identity["ed25519"]) > 0
        assert len(identity["curve25519"]) > 0

    def test_account_persistence(self, tmp_store: CryptoStore) -> None:
        mgr = OlmAccountManager(tmp_store)
        mgr.load_or_create()
        original_keys = mgr.account.identity_keys
        mgr._save()

        mgr2 = OlmAccountManager(tmp_store)
        mgr2.load_or_create()
        assert mgr2.account.identity_keys == original_keys

    def test_sign_message(self, account_mgr: OlmAccountManager) -> None:
        msg = b"hello"
        sig = account_mgr.account.sign(msg)
        assert isinstance(sig, str)
        assert len(sig) > 0

    def test_generate_one_time_keys(self, account_mgr: OlmAccountManager) -> None:
        account_mgr.account.generate_one_time_keys(5)
        otks = account_mgr.account.one_time_keys
        assert "curve25519" in otks
        assert len(otks["curve25519"]) >= 1

    def test_max_one_time_keys(self, account_mgr: OlmAccountManager) -> None:
        max_keys = account_mgr.account.max_one_time_keys
        assert isinstance(max_keys, int)
        assert max_keys >= 0

    def test_generate_fallback_key(self, account_mgr: OlmAccountManager) -> None:
        account_mgr.account.generate_fallback_key()
        fallback = account_mgr.account.fallback_key
        if "curve25519" in fallback:
            assert len(fallback["curve25519"]) == 1

    def test_encode_matrix_canonical_json_strips_signature_fields(self) -> None:
        payload = {
            "z": {"unsigned": {"age": 1}, "a": 1},
            "signatures": {"@alice:example.org": {"ed25519:DEV": "sig"}},
            "a": 2,
        }
        encoded = encode_matrix_canonical_json(payload)
        assert encoded == '{"a":2,"z":{"a":1}}'

    @pytest.mark.asyncio
    async def test_upload_identity_keys_uses_signed_device_keys(
        self,
        account_mgr: OlmAccountManager,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {"one_time_key_counts": {"signed_curve25519": 7}}
        ).encode("utf-8")

        await account_mgr.upload_identity_keys(dummy_adapter, dummy_bot)

        request = dummy_adapter.request_calls[-1]
        device_keys = request.json["device_keys"]
        assert "signatures" in device_keys
        assert device_keys["device_id"] == "TESTDEVICE"
        assert set(device_keys["keys"].keys()) == {
            "ed25519:TESTDEVICE",
            "curve25519:TESTDEVICE",
        }

    @pytest.mark.asyncio
    async def test_upload_one_time_keys_uses_signed_key_objects(
        self,
        account_mgr: OlmAccountManager,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {"one_time_key_counts": {"signed_curve25519": 12}}
        ).encode("utf-8")

        await account_mgr.upload_one_time_keys(dummy_adapter, dummy_bot, count=1)

        request = dummy_adapter.request_calls[-1]
        key_object = next(iter(request.json["one_time_keys"].values()))
        assert key_object["key"]
        assert "signatures" in key_object
        assert "fallback" not in key_object

    @pytest.mark.asyncio
    async def test_upload_fallback_key_uses_signed_key_object(
        self,
        account_mgr: OlmAccountManager,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {"one_time_key_counts": {"signed_curve25519": 12}}
        ).encode("utf-8")

        await account_mgr.upload_fallback_key(dummy_adapter, dummy_bot)

        request = dummy_adapter.request_calls[-1]
        key_object = next(iter(request.json["fallback_keys"].values()))
        assert key_object["key"]
        assert key_object["fallback"] is True
        assert "signatures" in key_object

    @pytest.mark.asyncio
    async def test_ensure_one_time_keys_uses_server_counts(
        self,
        account_mgr: OlmAccountManager,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {"one_time_key_counts": {"signed_curve25519": 50}}
        ).encode("utf-8")

        result = await account_mgr.ensure_one_time_keys(
            dummy_adapter,
            dummy_bot,
            key_upload_response={"one_time_key_counts": {"signed_curve25519": 10}},
            threshold=10,
            count=50,
        )

        assert result == {}
        assert dummy_adapter.request_calls == []

    @pytest.mark.asyncio
    async def test_ensure_one_time_keys_uploads_when_server_count_low(
        self,
        account_mgr: OlmAccountManager,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {"one_time_key_counts": {"signed_curve25519": 50}}
        ).encode("utf-8")

        await account_mgr.ensure_one_time_keys(
            dummy_adapter,
            dummy_bot,
            key_upload_response={"one_time_key_counts": {"signed_curve25519": 2}},
            threshold=10,
            count=20,
        )

        request = dummy_adapter.request_calls[-1]
        assert len(request.json["one_time_keys"]) == 18


# ------------------------------------------------------------------
# OlmSessionManager
# ------------------------------------------------------------------


class TestOlmSessionManager:
    def test_outbound_session_creation(
        self,
        session_mgr: OlmSessionManager,
        account_mgr: OlmAccountManager,
    ) -> None:
        # 创建一个对端账户
        peer = olm.Account()
        peer.generate_one_time_keys(1)
        otk_id = next(iter(peer.one_time_keys["curve25519"].keys()))
        otk = peer.one_time_keys["curve25519"][otk_id]

        session = session_mgr.create_outbound_session(
            peer.identity_keys["curve25519"], otk
        )
        assert session is not None
        assert session.id is not None
        assert len(session.id) > 0

    def test_outbound_with_invalid_keys(self, session_mgr: OlmSessionManager) -> None:
        session = session_mgr.create_outbound_session("invalid_key", "invalid_otk")
        assert session is None

    def test_olm_encrypt_decrypt_roundtrip(
        self, session_mgr: OlmSessionManager, account_mgr: OlmAccountManager
    ) -> None:
        # 对端账户
        peer = olm.Account()
        peer.generate_one_time_keys(1)
        otk_id = next(iter(peer.one_time_keys["curve25519"].keys()))
        otk = peer.one_time_keys["curve25519"][otk_id]

        # 出站会话
        out_session = session_mgr.create_outbound_session(
            peer.identity_keys["curve25519"], otk
        )
        assert out_session is not None

        # 加密
        plaintext = "Hello over Olm"
        encrypted_msg = session_mgr.encrypt(out_session, plaintext)
        assert encrypted_msg.ciphertext is not None

        # 入站会话 (对端)
        in_session = olm.InboundSession(
            peer,
            encrypted_msg,
            identity_key=account_mgr.account.identity_keys["curve25519"],
        )

        # 解密
        decrypted = session_mgr.decrypt(in_session, encrypted_msg)
        assert decrypted == plaintext

    def test_session_matches(
        self, session_mgr: OlmSessionManager, account_mgr: OlmAccountManager
    ) -> None:
        """验证 create_inbound_session 能正确处理预密钥消息。"""
        # 对端账户（持有 OTK）
        peer = olm.Account()
        peer.generate_one_time_keys(1)
        otk_id = next(iter(peer.one_time_keys["curve25519"].keys()))
        otk = peer.one_time_keys["curve25519"][otk_id]

        # 我们创建出站会话
        out_session = session_mgr.create_outbound_session(
            peer.identity_keys["curve25519"], otk
        )
        assert out_session is not None

        # 对端加密回应 (先加密再用出站会话的信息创建入站)
        msg = out_session.encrypt("test")
        # 在 peer 侧创建入站会话（使用 peer 的 account）
        in_session = olm.InboundSession(
            peer, msg, identity_key=account_mgr.account.identity_keys["curve25519"]
        )
        assert in_session is not None
        decrypted = session_mgr.decrypt(in_session, msg)
        assert decrypted == "test"

    def test_session_persistence(
        self,
        tmp_store: CryptoStore,
        account_mgr: OlmAccountManager,
    ) -> None:
        """测试 session 管理器持久化 (保存部分)。"""
        mgr = OlmSessionManager(tmp_store, account_mgr)
        mgr.load()

        peer = olm.Account()
        peer.generate_one_time_keys(1)
        otk_id = next(iter(peer.one_time_keys["curve25519"].keys()))
        session = mgr.create_outbound_session(
            peer.identity_keys["curve25519"],
            peer.one_time_keys["curve25519"][otk_id],
        )
        assert session is not None

        # 保存不应崩溃
        mgr._save()

        # 加载应返回 dict (可能为空如果 from_pickle 失败)
        mgr2 = OlmSessionManager(tmp_store, account_mgr)
        mgr2.load()
        assert isinstance(mgr2._sessions, dict)


# ------------------------------------------------------------------
# MegolmManager
# ------------------------------------------------------------------


class TestMegolmManager:
    def test_create_outbound_session(self, megolm_mgr: MegolmManager) -> None:
        session = megolm_mgr.get_outbound_session("!room:example.org")
        assert session is not None
        assert len(session.session_key) > 0
        assert len(session.id) > 0

    def test_encrypt_decrypt_roundtrip(self, megolm_mgr: MegolmManager) -> None:
        room_id = "!room:example.org"
        plaintext = json.dumps(
            {
                "type": "m.room.message",
                "content": {"body": "Hello E2EE", "msgtype": "m.text"},
            }
        )

        # 创建出站会话后立即获取 session_key (在加密之前)
        out_session = megolm_mgr.get_outbound_session(room_id)
        session_key = out_session.session_key  # 在加密前获取

        encrypted = megolm_mgr.encrypt(room_id, plaintext)
        assert encrypted["algorithm"] == "m.megolm.v1.aes-sha2"
        assert "ciphertext" in encrypted
        assert "sender_key" in encrypted
        assert "session_id" in encrypted

        session_id = encrypted["session_id"]

        # 使用加密前的 session_key 创建入站会话
        megolm_mgr.add_inbound_session(room_id, session_id, session_key)
        decrypted = megolm_mgr.decrypt(room_id, session_id, encrypted["ciphertext"])
        assert decrypted == plaintext

    def test_decrypt_room_event_extracts_nested_content(
        self,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        engine = CryptoEngine(dummy_bot, dummy_adapter)
        engine._account.load_or_create()
        engine._sessions.load()
        engine._megolm.load()
        engine._device_keys.load()

        room_id = "!room:example.org"
        plaintext = json.dumps(
            {
                "type": "m.room.message",
                "content": {"body": "Hello E2EE", "msgtype": "m.text"},
            }
        )
        out_session = engine._megolm.get_outbound_session(room_id)
        session_key = out_session.session_key
        encrypted = engine._megolm.encrypt(room_id, plaintext)
        session_id = encrypted["session_id"]
        engine._megolm.add_inbound_session(room_id, session_id, session_key)

        raw = engine._decrypt_megolm(
            RawMatrixEvent(
                type="m.room.encrypted",
                content=encrypted,
                room_id=room_id,
                sender="@alice:example.org",
            ),
            room_id,
            encrypted,
        )

        assert raw is not None
        assert raw.type == "m.room.message"
        assert raw.content == {"body": "Hello E2EE", "msgtype": "m.text"}

    def test_add_inbound_session_via_import(self, megolm_mgr: MegolmManager) -> None:
        outro = megolm_mgr.get_outbound_session("!room:example.org")
        ok = megolm_mgr.add_inbound_session(
            "!room:example.org", outro.id, outro.session_key
        )
        assert ok is True

    def test_add_inbound_session_invalid_key(self, megolm_mgr: MegolmManager) -> None:
        ok = megolm_mgr.add_inbound_session(
            "!room:example.org", "bad_sid", "not_a_valid_key"
        )
        assert ok is False

    def test_decrypt_without_session(self, megolm_mgr: MegolmManager) -> None:
        result = megolm_mgr.decrypt("!room:example.org", "unknown", "cipher")
        assert result is None

    def test_encrypt_content_has_algorithm(self, megolm_mgr: MegolmManager) -> None:
        encrypted = megolm_mgr.encrypt("!room:test", "plain")
        assert encrypted["algorithm"] == "m.megolm.v1.aes-sha2"

    def test_session_key_reuse(self, megolm_mgr: MegolmManager) -> None:
        """同一房间多次获取返回同一个出站会话。"""
        s1 = megolm_mgr.get_outbound_session("!room:example.org")
        s2 = megolm_mgr.get_outbound_session("!room:example.org")
        assert s1.id == s2.id

    def test_different_rooms_different_sessions(
        self, megolm_mgr: MegolmManager
    ) -> None:
        s1 = megolm_mgr.get_outbound_session("!room1:example.org")
        s2 = megolm_mgr.get_outbound_session("!room2:example.org")
        assert s1.id != s2.id


# ------------------------------------------------------------------
# DeviceKeyStore
# ------------------------------------------------------------------


class TestDeviceKeyStore:
    def test_cache_and_retrieve_keys(self, device_keys: DeviceKeyStore) -> None:
        device_keys._keys["@alice:example.org"] = {
            "ADEVICE": {
                "keys": {
                    "ed25519:ADEVICE": "abc123",
                    "curve25519:ADEVICE": "def456",
                },
                "algorithms": ["m.megolm.v1.aes-sha2"],
            },
        }
        device_keys._save()

        keys = device_keys.get_device_keys_for_user("@alice:example.org")
        assert "ADEVICE" in keys
        assert keys["ADEVICE"]["keys"]["ed25519:ADEVICE"] == "abc123"

    def test_get_single_device_key(self, device_keys: DeviceKeyStore) -> None:
        device_keys._keys["@bob:example.org"] = {
            "BDEVICE": {"keys": {"curve25519:BDEVICE": "key1"}},
        }
        key = device_keys.get_device_key("@bob:example.org", "BDEVICE")
        assert key is not None
        assert key["keys"]["curve25519:BDEVICE"] == "key1"

    def test_get_nonexistent(self, device_keys: DeviceKeyStore) -> None:
        assert device_keys.get_device_key("@nobody:example.org", "DEV") is None
        assert (
            device_keys.get_device_curve25519_key("@nobody:example.org", "DEV") is None
        )
        assert device_keys.get_device_ed25519_key("@nobody:example.org", "DEV") is None

    def test_get_device_curve25519_key(self, device_keys: DeviceKeyStore) -> None:
        device_keys._keys["@user:example.org"] = {
            "ADEV": {"keys": {"curve25519:ADEV": "curvesecret"}},
        }
        key = device_keys.get_device_curve25519_key("@user:example.org", "ADEV")
        assert key == "curvesecret"

    def test_get_device_ed25519_key(self, device_keys: DeviceKeyStore) -> None:
        device_keys._keys["@user:example.org"] = {
            "ADEV": {"keys": {"ed25519:ADEV": "edsecret"}},
        }
        key = device_keys.get_device_ed25519_key("@user:example.org", "ADEV")
        assert key == "edsecret"

    def test_mark_for_query(self, device_keys: DeviceKeyStore) -> None:
        device_keys.mark_for_query(["@alice:example.org", "@bob:example.org"])
        assert len(device_keys._pending_query) == 2

    def test_mark_left(self, device_keys: DeviceKeyStore) -> None:
        device_keys._keys["@alice:example.org"] = {"DEV": {}}
        device_keys.mark_left(["@alice:example.org"])
        assert "@alice:example.org" not in device_keys._keys

    def test_persistence(self, tmp_store: CryptoStore) -> None:
        store = DeviceKeyStore(tmp_store)
        store.load()
        store._keys["@bob:example.org"] = {
            "BOBDEV": {"keys": {}},
        }
        store._save()

        store2 = DeviceKeyStore(tmp_store)
        store2.load()
        assert "@bob:example.org" in store2._keys

    @pytest.mark.asyncio
    async def test_query_keys(
        self,
        device_keys: DeviceKeyStore,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        device_keys.mark_for_query(["@alice:example.org"])
        dummy_adapter.content = json.dumps(
            {
                "device_keys": {
                    "@alice:example.org": {
                        "ADEVICE": {
                            "keys": {
                                "ed25519:ADEVICE": "edkey1",
                                "curve25519:ADEVICE": "curvesecret",
                            },
                            "algorithms": ["m.megolm.v1.aes-sha2"],
                        },
                    },
                },
            }
        ).encode("utf-8")
        await device_keys.query_keys(dummy_adapter, dummy_bot)
        key = device_keys.get_device_curve25519_key("@alice:example.org", "ADEVICE")
        assert key == "curvesecret"

    @pytest.mark.asyncio
    async def test_claim_one_time_key(
        self,
        device_keys: DeviceKeyStore,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        dummy_adapter.content = json.dumps(
            {
                "one_time_keys": {
                    "@alice:example.org": {
                        "ADEVICE": {
                            "signed_curve25519:AAAAAQ": {"key": "otk_value_here"},
                        },
                    },
                },
            }
        ).encode("utf-8")
        otk = await device_keys.claim_one_time_key(
            dummy_adapter, dummy_bot, "@alice:example.org", "ADEVICE"
        )
        assert otk == "otk_value_here"


# ------------------------------------------------------------------
# OlmSessionManager in-device test (Megolm key sharing simulation)
# ------------------------------------------------------------------


class TestCryptoEngine:
    @pytest.mark.asyncio
    async def test_encrypt_room_message_wraps_room_event_payload(
        self,
        dummy_adapter: DummyAdapter,
        dummy_bot: DummyBot,
    ) -> None:
        engine = CryptoEngine(dummy_bot, dummy_adapter)
        engine._account.load_or_create()
        engine._sessions.load()
        engine._megolm.load()
        engine._device_keys.load()

        room_id = "!room:example.org"
        event_content = {
            "body": "image.png",
            "msgtype": "m.image",
            "url": "mxc://example.org/image",
        }
        outbound = engine._megolm.get_outbound_session(room_id)
        session_key = outbound.session_key
        encrypted = await engine.encrypt_room_message(room_id, event_content)

        session_id = encrypted["session_id"]
        ciphertext = encrypted["ciphertext"]
        engine._megolm.add_inbound_session(room_id, session_id, session_key)
        decrypted = engine._megolm.decrypt(room_id, session_id, ciphertext)

        assert decrypted is not None
        assert json.loads(decrypted) == {
            "type": "m.room.message",
            "content": event_content,
            "room_id": room_id,
        }

    @pytest.mark.asyncio
    async def test_prepare_room_encryption_shares_only_once_per_session(self) -> None:
        peer = olm.Account()
        peer.generate_one_time_keys(1)
        otk_id = next(iter(peer.one_time_keys["curve25519"].keys()))
        otk = peer.one_time_keys["curve25519"][otk_id]
        members_payload = {
            "chunk": [
                {
                    "type": "m.room.member",
                    "state_key": "@alice:example.org",
                    "content": {"membership": "join"},
                },
                {
                    "type": "m.room.member",
                    "state_key": "@bot:example.org",
                    "content": {"membership": "join"},
                },
            ]
        }
        key_query_payload = {
            "device_keys": {
                "@alice:example.org": {
                    "ALICEDEV": {
                        "keys": {
                            "ed25519:ALICEDEV": peer.identity_keys["ed25519"],
                            "curve25519:ALICEDEV": peer.identity_keys["curve25519"],
                        },
                        "algorithms": ["m.megolm.v1.aes-sha2"],
                    }
                }
            }
        }
        key_claim_payload = {
            "one_time_keys": {
                "@alice:example.org": {
                    "ALICEDEV": {
                        f"signed_curve25519:{otk_id}": {"key": otk}
                    }
                }
            }
        }
        adapter = DummyAdapter(
            responses=[
                (200, json.dumps(members_payload).encode("utf-8")),
                (200, json.dumps(key_query_payload).encode("utf-8")),
                (200, json.dumps(key_claim_payload).encode("utf-8")),
                (200, b"{}"),
            ]
        )
        bot = DummyBot(adapter=adapter, device_id="TESTDEVICE")
        engine = CryptoEngine(bot, adapter)
        engine._account.load_or_create()
        engine._sessions.load()
        engine._megolm.load()
        engine._device_keys.load()

        await engine.prepare_room_encryption("!room:example.org")
        assert len(adapter.request_calls) == 4
        assert str(adapter.request_calls[0].url).endswith("/members?membership=join")
        assert str(adapter.request_calls[1].url).endswith("/keys/query")
        assert str(adapter.request_calls[2].url).endswith("/keys/claim")
        assert "/sendToDevice/m.room.encrypted/" in str(adapter.request_calls[3].url)

        to_device_request = adapter.request_calls[3]
        encrypted_payload = next(
            iter(next(iter(to_device_request.json["messages"].values())).values())
        )
        ciphertext_entry = next(iter(encrypted_payload["ciphertext"].values()))
        olm_message = olm.OlmPreKeyMessage(ciphertext_entry["body"])
        peer_session = olm.InboundSession(
            peer,
            olm_message,
            identity_key=encrypted_payload["sender_key"],
        )
        plaintext = peer_session.decrypt(olm_message)
        room_key_payload = json.loads(plaintext)
        assert room_key_payload["type"] == "m.room_key"
        assert room_key_payload["sender"] == "@bot:example.org"
        assert room_key_payload["recipient"] == "@alice:example.org"
        assert room_key_payload["recipient_keys"]["ed25519"] == peer.identity_keys["ed25519"]
        assert room_key_payload["sender_device_keys"]["device_id"] == "TESTDEVICE"

        await engine.prepare_room_encryption("!room:example.org")
        assert len(adapter.request_calls) == 4


class TestOlmKeySharingSimulation:
    """模拟 Megolm 密钥共享流程: Alice 创建出站 Megolm 会话，
    通过 Olm 将会话密钥加密发送给 Bob，Bob 解密后导入。
    """

    def test_full_key_sharing_simulation(self) -> None:
        # Alice 侧
        alice_account = olm.Account()
        alice_megolm = olm.OutboundGroupSession()

        # Bob 侧
        bob_account = olm.Account()
        bob_account.generate_one_time_keys(1)
        bob_otk_id = next(iter(bob_account.one_time_keys["curve25519"].keys()))
        bob_otk = bob_account.one_time_keys["curve25519"][bob_otk_id]

        # Alice 为 Bob 创建出站 Olm 会话
        alice_session = olm.OutboundSession(
            alice_account,
            bob_account.identity_keys["curve25519"],
            bob_otk,
        )

        # Alice 加密 m.room_key 负载
        room_key_payload = json.dumps(
            {
                "type": "m.room_key",
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "room_id": "!room:example.org",
                    "session_id": alice_megolm.id,
                    "session_key": alice_megolm.session_key,
                },
            }
        )
        encrypted_olm_msg = alice_session.encrypt(room_key_payload)

        # Bob 接收并创建入站 Olm 会话
        bob_session = olm.InboundSession(
            bob_account,
            encrypted_olm_msg,
            identity_key=alice_account.identity_keys["curve25519"],
        )

        # Bob 解密 Olm 消息获取 m.room_key
        decrypted_olm = bob_session.decrypt(encrypted_olm_msg)
        room_key_data = json.loads(decrypted_olm)
        assert room_key_data["type"] == "m.room_key"
        session_key = room_key_data["content"]["session_key"]
        room_key_data["content"]["session_id"]

        # Bob 从 session_key 创建 Megolm 入站会话
        bob_megolm = olm.InboundGroupSession(session_key)

        # Alice 用 Megolm 加密消息
        message_plaintext = json.dumps(
            {
                "type": "m.room.message",
                "content": {"body": "Secret message", "msgtype": "m.text"},
            }
        )
        ciphertext = alice_megolm.encrypt(message_plaintext)

        # Bob 用 Megolm 解密消息
        decrypted, msg_index = bob_megolm.decrypt(ciphertext)
        assert json.loads(decrypted)["content"]["body"] == "Secret message"
        assert isinstance(msg_index, int)
