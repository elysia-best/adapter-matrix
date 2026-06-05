"""MegolmManager — 管理 Megolm 群组棘轮会话。

Megolm 是 Matrix 用于群组加密的协议。每个房间维护一个出站会话
（用于加密发出的消息）和多个入站会话（用于解密收到的消息）。

会话密钥通过 Olm 加密的 to-device 消息在设备间共享。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import olm

from .device_keys import DeviceKeyStore
from .sessions import OlmSessionManager
from .store import CryptoStore
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


class MegolmManager:
    """管理 Megolm 入站和出站会话。

    出站会话: 每个房间一个活跃的 OutboundGroupSession，定期轮换
    入站会话: 从其他设备通过 m.room_key 共享的 InboundGroupSession
    """

    def __init__(
        self,
        store: CryptoStore,
        session_mgr: OlmSessionManager,
        device_keys: DeviceKeyStore,
    ) -> None:
        self._store = store
        self._session_mgr = session_mgr
        self._device_keys = device_keys
        self._inbound: dict[str, dict[str, olm.InboundGroupSession]] = {}
        self._outbound: dict[str, olm.OutboundGroupSession] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从存储的 session_key 重建所有入站和出站会话。"""
        stored_in = self._store.load_inbound_sessions()
        for room_id, sessions in stored_in.items():
            self._inbound.setdefault(room_id, {})
            for session_id, session_key in sessions.items():
                self._try_import_session(room_id, session_id, session_key)
        log("TRACE", f"已加载 {self._count_inbound()} 个入站 Megolm 会话")

        stored_out = self._store.load_outbound_sessions()
        for room_id in stored_out:
            self._outbound[room_id] = olm.OutboundGroupSession()
        log("TRACE", f"已为 {len(self._outbound)} 个房间创建出站 Megolm 会话")

    def _try_import_session(
        self, room_id: str, session_id: str, session_key: str
    ) -> None:
        """尝试从 session_key 导入入站会话，失败时记录警告。"""
        try:
            self._inbound[room_id][session_id] = olm.InboundGroupSession.import_session(
                session_key
            )
        except olm.OlmGroupSessionError as e:
            log(
                "WARNING",
                f"无法从 session_key 重建入站 Megolm 会话 {room_id}/{session_id}: {e}",
            )

    def _count_inbound(self) -> int:
        return sum(len(s) for s in self._inbound.values())

    # ------------------------------------------------------------------
    # 出站会话 (加密发送)
    # ------------------------------------------------------------------

    def get_outbound_session(self, room_id: str) -> olm.OutboundGroupSession:
        """获取房间的出站会话，如果不存在则创建新的。"""
        session = self._outbound.get(room_id)
        if session is None:
            session = olm.OutboundGroupSession()
            self._outbound[room_id] = session
            self._save_outbound()
        return session

    def get_outbound_session_id(self, room_id: str) -> str:
        """返回房间当前出站 Megolm 会话的 session_id。"""
        return self.get_outbound_session(room_id).id

    def encrypt(self, room_id: str, plaintext: str) -> dict:
        """用 Megolm 加密明文，返回 m.room.encrypted 事件的内容。

        加密后的内容包含 algorithm、ciphertext、sender_key 和 session_id。
        """
        session = self.get_outbound_session(room_id)
        ciphertext = session.encrypt(plaintext)
        return {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": ciphertext,
            "sender_key": self._account().identity_keys["curve25519"],
            "session_id": session.id,
            "device_id": "...",  # 将由 CryptoEngine 在调用时填充
        }

    def rotate_outbound_session(self, room_id: str) -> olm.OutboundGroupSession:
        """轮换房间的出站 Megolm 会话。

        定期轮换可以提高前向安全性：旧的 ratchet 值被丢弃后，
        即使密钥泄露也无法解密历史消息。
        """
        new_session = olm.OutboundGroupSession()
        old_session = self._outbound.pop(room_id, None)
        self._outbound[room_id] = new_session
        self._save_outbound()
        if old_session:
            log("TRACE", f"已轮换房间 {room_id} 的出站 Megolm 会话")
        return new_session

    # ------------------------------------------------------------------
    # 入站会话 (解密接收)
    # ------------------------------------------------------------------

    def add_inbound_session(
        self, room_id: str, session_id: str, session_key: str
    ) -> bool:
        """从共享的 session_key 创建入站 Megolm 会话。

        当通过 Olm to-device 消息收到 m.room_key 时调用此方法。
        session_key 是 OutboundGroupSession.session_key 的原始输出。

        Args:
            room_id: 房间 ID
            session_id: 会话 ID
            session_key: 原始 session_key（base64 编码）

        Returns:
            导入成功返回 True
        """
        try:
            session = olm.InboundGroupSession(session_key)
            self._inbound.setdefault(room_id, {})[session_id] = session
            self._save_inbound()
            log("TRACE", f"已导入入站 Megolm 会话 {room_id}/{session_id}")
            return True
        except olm.OlmGroupSessionError as e:
            log(
                "WARNING",
                f"导入入站 Megolm 会话失败 {room_id}/{session_id}: {e}",
            )
            return False

    def decrypt(self, room_id: str, session_id: str, ciphertext: str) -> str | None:
        """用匹配的入站会话解密密文。

        Args:
            room_id: 房间 ID
            session_id: 会话 ID（来自加密事件的 session_id 字段）
            ciphertext: base64 编码的密文

        Returns:
            解密后的明文字符串，如果找不到会话或解密失败返回 None
        """
        room_sessions = self._inbound.get(room_id)
        if room_sessions is None:
            log("TRACE", f"房间 {room_id} 没有入站 Megolm 会话")
            return None

        session = room_sessions.get(session_id)
        if session is None:
            log(
                "TRACE",
                f"房间 {room_id} 缺少入站 Megolm 会话 {session_id}",
            )
            return None

        try:
            plaintext, _message_index = session.decrypt(ciphertext)
            return plaintext
        except olm.OlmGroupSessionError as e:
            log(
                "WARNING",
                f"Megolm 解密失败 ({room_id}/{session_id}): {e}",
            )
            return None

    # ------------------------------------------------------------------
    # 密钥共享
    # ------------------------------------------------------------------

    async def share_session_key(
        self,
        adapter: Adapter,
        bot: Bot,
        room_id: str,
        device_id: str,  # noqa: ARG002
        members: list[str],
    ) -> int:
        """向房间的所有成员设备共享当前出站 Megolm 会话密钥。

        通过 Olm 加密的 m.room_key to-device 消息发送。
        这是首次向加密房间发送消息前的必要步骤。

        Args:
            adapter: Adapter 实例（用于 API 调用）
            bot: Bot 实例
            room_id: 目标房间 ID
            device_id: 本机的设备 ID
            members: 房间成员 user_id 列表
        """
        session = self.get_outbound_session(room_id)
        session_key = session.session_key
        session_id = session.id

        account = self._account()
        my_curve25519 = account.identity_keys["curve25519"]
        my_device_keys = self._session_mgr._account_mgr.build_device_keys(bot)
        my_ed25519 = account.identity_keys["ed25519"]

        # 收集要发送的 to-device 消息
        to_device_messages: dict[str, dict[str, dict]] = {}

        for user_id in members:
            if user_id == str(bot.user_id):
                continue

            devices = self._device_keys.get_device_keys_for_user(user_id)
            if not devices:
                log("TRACE", f"用户 {user_id} 没有缓存的设备密钥，跳过密钥共享")
                continue

            for dev_id, dev_info in devices.items():
                their_curve25519 = dev_info.get("keys", {}).get(f"curve25519:{dev_id}")
                if their_curve25519 is None:
                    continue

                # 索取一次性密钥
                one_time = await self._device_keys.claim_one_time_key(
                    adapter, bot, user_id, dev_id
                )
                if one_time is None:
                    # 尝试使用 fallback 密钥
                    log("TRACE", f"无法索取 {user_id}/{dev_id} 的 OTK")
                    continue

                # 创建出站 Olm 会话
                olm_session = self._session_mgr.create_outbound_session(
                    their_curve25519, one_time
                )
                if olm_session is None:
                    continue

                their_ed25519 = dev_info.get("keys", {}).get(f"ed25519:{dev_id}")
                if not isinstance(their_ed25519, str):
                    continue

                # 构建 m.room_key 负载
                room_key_payload = {
                    "content": {
                        "algorithm": "m.megolm.v1.aes-sha2",
                        "room_id": room_id,
                        "session_id": session_id,
                        "session_key": session_key,
                    },
                    "keys": {
                        "ed25519": my_ed25519,
                    },
                    "recipient": user_id,
                    "recipient_keys": {
                        "ed25519": their_ed25519,
                    },
                    "sender": str(bot.user_id),
                    "sender_device_keys": my_device_keys,
                    "type": "m.room_key",
                }
                import json

                plaintext = json.dumps(room_key_payload, ensure_ascii=False)
                encrypted_msg = self._session_mgr.encrypt(olm_session, plaintext)

                # 将加密的 Olm 消息添加到 to-device 集合中
                to_device_messages.setdefault(user_id, {})[dev_id] = {
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": my_curve25519,
                    "ciphertext": {
                        their_curve25519: {
                            "body": encrypted_msg.ciphertext,
                            "type": encrypted_msg.message_type,
                        },
                    },
                }

        if not to_device_messages:
            return 0

        import uuid

        txn_id = uuid.uuid4().hex
        await adapter._api_send_to_device(  # type: ignore[union-attr]
            bot,
            event_type="m.room.encrypted",
            txn_id=txn_id,
            messages=to_device_messages,
        )
        total_devices = sum(len(d) for d in to_device_messages.values())
        log(
            "TRACE",
            f"已向 {len(to_device_messages)} 个用户的 "
            f"{total_devices} 个设备共享房间 {room_id} 的 Megolm 密钥",
        )
        return total_devices

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _account(self) -> olm.Account:
        return self._session_mgr._account_mgr.account

    def _save_inbound(self) -> None:
        store_data: dict[str, dict[str, str]] = {}
        for room_id, sessions in self._inbound.items():
            store_data[room_id] = {}
            for session_id, session in sessions.items():
                try:
                    store_data[room_id][session_id] = session.export_session(
                        session.first_known_index
                    )
                except olm.OlmGroupSessionError:
                    store_data[room_id][session_id] = session.id
        self._store.save_inbound_sessions(store_data)

    def _save_outbound(self) -> None:
        store_data: dict[str, dict[str, object]] = {}
        for room_id, session in self._outbound.items():
            store_data[room_id] = {
                "session_id": session.id,
                "session_key": session.session_key,
                "message_index": session.message_index,
            }
        self._store.save_outbound_sessions(store_data)
