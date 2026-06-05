"""OlmSessionManager — 管理 1:1 Olm 双棘轮会话。

Olm 会话用于在设备之间建立安全的点对点通道，
主要通过 to-device 消息交换 Megolm 会话密钥。
"""

from __future__ import annotations

from typing import cast

import olm

from .account import OlmAccountManager
from .store import CryptoStore
from ..utils import log


class OlmSessionManager:
    """管理 Olm 入站和出站会话的生命周期。

    入站会话：从对端发来的 OlmPreKeyMessage 创建
    出站会话：使用对端的 identity_key 和 one_time_key 创建
    """

    def __init__(self, store: CryptoStore, account_mgr: OlmAccountManager) -> None:
        self._store = store
        self._account_mgr = account_mgr
        # {session_id: olm.Session 子类实例}
        self._sessions: dict[str, olm.Session] = {}

    def load(self) -> None:
        """从持久化存储加载所有 Olm 会话。"""
        loaded = self._store.load_sessions()
        if loaded:
            self._sessions = loaded
            log("TRACE", f"已加载 {len(self._sessions)} 个 Olm 会话")
        else:
            self._sessions = {}

    def _save(self) -> None:
        self._store.save_sessions(self._sessions)

    def create_inbound_session(
        self, message: olm.OlmPreKeyMessage, identity_key: str | None = None
    ) -> olm.Session | None:
        """从 Olm 预密钥消息创建入站会话。

        Args:
            message: 对端发来的 OlmPreKeyMessage
            identity_key: 发送者的 Curve25519 身份密钥（可选，用于验证）

        Returns:
            创建成功返回 Session 对象，失败返回 None
        """
        try:
            session = olm.InboundSession(
                self._account_mgr.account, message, identity_key=identity_key
            )
            session_id = session.id
            self._sessions[session_id] = session
            self._save()
            log("TRACE", f"已创建入站 Olm 会话 {session_id}")
            return session
        except olm.OlmSessionError as e:
            log("WARNING", f"创建入站 Olm 会话失败: {e}")
            return None

    def create_outbound_session(
        self, their_identity_key: str, their_one_time_key: str
    ) -> olm.Session | None:
        """使用对端的身份密钥和一次性密钥创建出站 Olm 会话。

        Args:
            their_identity_key: 对端设备的 Curve25519 身份密钥 (base64)
            their_one_time_key: 对端设备的一次性 Curve25519 密钥 (base64)

        Returns:
            创建成功返回 OutboundSession 对象，失败返回 None
        """
        try:
            session = olm.OutboundSession(
                self._account_mgr.account,
                their_identity_key,
                their_one_time_key,
            )
            session_id = session.id
            self._sessions[session_id] = session
            self._save()
            log("TRACE", f"已创建出站 Olm 会话 {session_id}")
            return session
        except olm.OlmSessionError as e:
            log("WARNING", f"创建出站 Olm 会话失败: {e}")
            return None

    def find_inbound_session(self, message: olm.OlmPreKeyMessage) -> olm.Session | None:
        """在已有会话中查找匹配此消息的入站会话。"""
        for session in self._sessions.values():
            try:
                if session.matches(message):
                    return session
            except olm.OlmSessionError:
                continue
        return None

    def decrypt_to_device_message(
        self, ciphertext_body: str, message_type: int, sender_key: str
    ) -> str | None:
        """解密 Olm 加密的 to-device 消息，返回明文 JSON 字符串。

        Matrix to-device 消息有两种形式:
        - type=0: OlmPreKeyMessage（首次通信，创建新会话）
        - type=1: OlmMessage（已有会话的后续消息）

        ciphertext_body 是 base64 编码的 Olm 消息体（来自 m.room.encrypted
        to-device 事件的 ciphertext 字段）。
        """
        if message_type == 0:
            message: olm.OlmMessage | olm.OlmPreKeyMessage = olm.OlmPreKeyMessage(
                ciphertext_body
            )
            session = self.find_inbound_session(message)
            if session is None:
                session = self.create_inbound_session(message, sender_key)
                if session is None:
                    return None
        elif message_type == 1:
            message = olm.OlmMessage(ciphertext_body)
            message = cast("olm.OlmPreKeyMessage", message)  # type: ignore[no-redef]
            session = self.find_inbound_session(message)
            if session is None:
                log("TRACE", f"找不到匹配的 Olm 会话 (sender_key={sender_key[:12]}...)")
                return None
        else:
            log("WARNING", f"未知 Olm 消息类型: {message_type}")
            return None

        return self.decrypt(session, message)

    def encrypt(self, session: olm.Session, plaintext: str) -> olm._OlmMessage:
        """用 Olm 会话加密明文，返回 OlmPreKeyMessage 或 OlmMessage。"""
        return session.encrypt(plaintext)

    def decrypt(self, session: olm.Session, message: olm._OlmMessage) -> str | None:
        """用 Olm 会话解密密文，返回明文字符串。"""
        try:
            return session.decrypt(message)
        except olm.OlmSessionError as e:
            log("WARNING", f"Olm 解密失败: {e}")
            return None
