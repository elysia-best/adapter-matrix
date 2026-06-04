"""CryptoEngine — E2EE 加密引擎，编排所有加密子系统。

CryptoEngine 是加密子系统的顶层入口，负责:
- 在 Bot 启动时初始化 Olm 账户和密钥上传
- 在每个同步周期处理 device_lists 和 to_device 事件
- 加密发出的房间消息和解密收到的房间消息
- 管理房间加密状态和密钥共享

E2EE 是 opt-in 的: 只有当 BotInfo 配置了 e2ee_store_path
或 matrix_token_store_path 时才会初始化加密引擎。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .account import OlmAccountManager
from .device_keys import DeviceKeyStore
from .megolm import MegolmManager
from .recovery import KeyRecovery
from .sessions import OlmSessionManager
from .store import CryptoStore
from ..api.model import RawMatrixEvent
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


class CryptoEngine:
    """E2EE 加密引擎。

    属性:
        _bot: Bot 实例
        _adapter: Adapter 实例 (用于 API 调用)
        _store: 持久化存储
        _account: Olm 账户管理器
        _sessions: Olm 会话管理器
        _megolm: Megolm 群组会话管理器
        _device_keys: 设备密钥缓存
        _recovery: 密钥备份恢复
        _room_encrypted: 房间加密状态缓存 {room_id: {"encrypted": bool, "algorithm": str}}
    """

    def __init__(self, bot: Bot, adapter: Adapter) -> None:
        self._bot = bot
        self._adapter = adapter

        # 确定存储路径
        bot_info = bot.bot_info
        if bot_info.e2ee_store_path:
            store_dir = Path(bot_info.e2ee_store_path)
        elif adapter.matrix_config.matrix_token_store_path:  # type: ignore[union-attr]
            base = (
                Path(adapter.matrix_config.matrix_token_store_path).parent  # type: ignore[union-attr]
            )
            h = hashlib.sha256(bot_info.homeserver.encode()).hexdigest()[:8]
            user_part = str(bot.user_id).split(":")[0].lstrip("@")
            store_dir = base / f"e2ee_{h}_{user_part}"
        else:
            msg = "E2EE 存储路径或 matrix_token_store_path 未配置，无法初始化加密引擎"
            raise RuntimeError(msg)

        self._store = CryptoStore(store_dir)
        self._account = OlmAccountManager(self._store)
        self._sessions = OlmSessionManager(self._store, self._account)
        self._device_keys = DeviceKeyStore(self._store)
        self._megolm = MegolmManager(self._store, self._sessions, self._device_keys)
        self._recovery = KeyRecovery(self._store, self._megolm)

        # 房间加密状态缓存
        self._room_encrypted: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """在 Bot 启动时初始化加密引擎。

        执行步骤:
        1. 加载或创建 OlmAccount (Ed25519 + Curve25519 密钥对)
        2. 加载持久化的 Olm 会话和 Megolm 会话
        3. 加载设备密钥缓存和房间加密状态
        4. 上传设备身份密钥到 homeserver
        5. 确保有足够的一次性密钥 (OTK)
        6. 上传 fallback 密钥作为备用
        7. 如果配置了 MATRIX_RECOVERY_CODE，恢复备份的 Megolm 会话
        """
        # 1-3: 加载持久化状态
        self._account.load_or_create()
        self._sessions.load()
        self._megolm.load()
        self._device_keys.load()
        self._room_encrypted = self._store.load_room_state()

        # 4-6: 上传密钥到 homeserver
        try:
            await self._account.upload_identity_keys(self._adapter, self._bot)
        except Exception as e:
            log("WARNING", f"上传设备身份密钥失败: {type(e).__name__}: {e}")

        try:
            await self._account.ensure_one_time_keys(self._adapter, self._bot)
        except Exception as e:
            log("WARNING", f"补充一次性密钥失败: {type(e).__name__}: {e}")

        try:
            await self._account.upload_fallback_key(self._adapter, self._bot)
        except Exception as e:
            log("WARNING", f"上传 fallback 密钥失败: {type(e).__name__}: {e}")

        # 7: 从恢复码恢复 Megolm 会话
        recovery_code = self._bot.bot_info.recovery_code
        if recovery_code:
            try:
                count = await self._recovery.recover_from_backup(
                    self._adapter, self._bot, recovery_code
                )
                log("INFO", f"从密钥备份恢复了 {count} 个 Megolm 会话")
            except Exception as e:
                log(
                    "WARNING",
                    f"密钥备份恢复失败: {type(e).__name__}: {e}",
                )

        log("INFO", "E2EE 加密引擎初始化完成")

    # ------------------------------------------------------------------
    # 房间加密状态
    # ------------------------------------------------------------------

    def is_room_encrypted(self, room_id: str) -> bool:
        """检查房间是否已启用端到端加密。

        只有当房间的 m.room.encryption 状态事件被处理后
        才会标记为已加密。
        """
        state = self._room_encrypted.get(room_id)
        return state is not None and state.get("encrypted", False)

    def mark_room_as_encrypted(self, room_id: str, algorithm: str) -> None:
        """根据 m.room.encryption 状态事件标记房间为已加密。

        当同步循环在 state.events 中检测到此事件时调用。
        """
        self._room_encrypted[room_id] = {
            "encrypted": True,
            "algorithm": algorithm,
        }
        self._store.save_room_state(self._room_encrypted)
        log("TRACE", f"房间 {room_id} 已标记为加密 (algorithm={algorithm})")

    # ------------------------------------------------------------------
    # 设备列表处理
    # ------------------------------------------------------------------

    async def handle_device_lists(self, changed: list[str], left: list[str]) -> None:
        """处理同步响应中的 device_lists 变更。

        changed: 设备列表发生变化的用户 ID 列表 → 查询新密钥
        left: 离开的用户 ID 列表 → 清理本地缓存
        """
        if changed:
            self._device_keys.mark_for_query(changed)
            await self._device_keys.query_keys(self._adapter, self._bot)
        if left:
            self._device_keys.mark_left(left)

    # ------------------------------------------------------------------
    # To-Device 事件处理
    # ------------------------------------------------------------------

    async def handle_to_device_event(self, raw: RawMatrixEvent) -> None:
        """处理 to-device 事件。

        主要处理以下类型:
        - m.room_key: 导入 Megolm 入站会话密钥
        - m.forwarded_room_key: 转发来的 Megolm 会话密钥
        - m.room_key_request: 密钥请求 (可选地重新发送密钥)
        - m.room_key.withheld: 密钥被拒绝的通知
        """
        event_type = raw.type
        content = raw.content
        sender = str(raw.sender) if raw.sender else "unknown"

        if event_type == "m.room_key":
            await self._handle_room_key(content)
        elif event_type == "m.forwarded_room_key":
            await self._handle_forwarded_room_key(content)
        elif event_type == "m.room_key_request":
            await self._handle_key_request(sender, content)
        elif event_type and event_type.startswith("m.room_key.withheld"):
            log(
                "WARNING",
                f"密钥被拒绝来自 {sender}: "
                f"room={content.get('room_id')}, "
                f"reason={content.get('code')}",
            )
        elif event_type and event_type.startswith("m.key"):
            # 密钥验证消息 — 暂时忽略
            log("TRACE", f"密钥验证消息来自 {sender}: {event_type}")

    async def _handle_room_key(self, content: dict[str, Any]) -> None:
        """处理 m.room_key: 导入 Megolm 入站会话密钥。

        m.room_key 负载结构:
            algorithm: "m.megolm.v1.aes-sha2"
            room_id: 房间 ID
            session_id: 会话 ID
            session_key: 导出的 session_key (base64)
        """
        algorithm = content.get("algorithm")
        if algorithm != "m.megolm.v1.aes-sha2":
            return

        room_id = content.get("room_id")
        session_id = content.get("session_id")
        session_key = content.get("session_key")

        if not (
            isinstance(room_id, str)
            and isinstance(session_id, str)
            and isinstance(session_key, str)
        ):
            log("WARNING", f"m.room_key 缺少必要字段: {content}")
            return

        if self._megolm.add_inbound_session(room_id, session_id, session_key):
            log("TRACE", f"已导入入站 Megolm 会话 {room_id}/{session_id}")

    async def _handle_forwarded_room_key(self, content: dict[str, Any]) -> None:
        """处理 m.forwarded_room_key: 转发来的 Megolm 会话密钥。

        格式与 m.room_key 相同，但包含额外的转发链信息。
        """
        await self._handle_room_key(content)

    async def _handle_key_request(self, sender: str, content: dict[str, Any]) -> None:
        """处理 m.room_key_request: 其他设备请求我们共享密钥。

        简单实现: 记录请求但不自动重新发送。
        自动重新发送可能导致密钥共享循环。
        """
        room_id = content.get("room_id")
        if room_id:
            log(
                "TRACE",
                f"收到来自 {sender} 的密钥请求 (room={room_id})",
            )

    # ------------------------------------------------------------------
    # 消息加密/解密
    # ------------------------------------------------------------------

    async def encrypt_room_message(
        self, room_id: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        """用 Megolm 加密房间消息内容。

        Args:
            room_id: 目标房间 ID
            content: 明文消息内容 (Matrix 消息格式)

        Returns:
            m.room.encrypted 事件的内容字典
        """
        plaintext = json.dumps(content, ensure_ascii=False)
        encrypted = self._megolm.encrypt(room_id, plaintext)
        # 填充 device_id
        device_id = self._bot.device_id or ""
        encrypted["device_id"] = device_id
        return encrypted

    async def decrypt_room_event(
        self, raw: RawMatrixEvent, *, room_id: str
    ) -> RawMatrixEvent | None:
        """解密 m.room.encrypted 事件，返回解密的 RawMatrixEvent。

        Args:
            raw: 原始的 m.room.encrypted 事件
            room_id: 房间 ID

        Returns:
            解密后的 RawMatrixEvent (如同收到了明文 m.room.message)，
            如果解密失败则返回 None
        """
        content = raw.content
        algorithm = content.get("algorithm", "")

        if algorithm == "m.megolm.v1.aes-sha2":
            return self._decrypt_megolm(raw, room_id, content)
        if algorithm == "m.olm.v1.curve25519-aes-sha2":
            # Olm 加密的房间事件——通常是设备间的密钥传输，
            # 不包含用户消息，无需分派
            log("TRACE", "收到 Olm 加密的房间事件，跳过分派")
            return None
        log("WARNING", f"未知加密算法: {algorithm}")
        return None

    def _decrypt_megolm(
        self, raw: RawMatrixEvent, room_id: str, content: dict[str, Any]
    ) -> RawMatrixEvent | None:
        """解密 Megolm 加密的事件。

        解密成功后，将明文的 JSON 内容解析为 RawMatrixEvent 返回。
        原始事件的元数据 (event_id, sender, origin_server_ts) 得以保留。
        """
        session_id = content.get("session_id")
        ciphertext = content.get("ciphertext")
        sender_key = content.get("sender_key")

        if not (
            isinstance(session_id, str)
            and isinstance(ciphertext, str)
            and isinstance(sender_key, str)
        ):
            log("WARNING", f"Megolm 加密事件不完整: {content}")
            return None

        plaintext = self._megolm.decrypt(room_id, session_id, ciphertext)
        if plaintext is None:
            # 没有入站会话 → 可能还没收到密钥
            log(
                "TRACE",
                f"无法解密 Megolm 消息 {room_id}/{session_id}（缺少入站会话）",
            )
            return None

        # 解码解密后的 JSON 内容
        try:
            decrypted_content = json.loads(plaintext)
        except json.JSONDecodeError:
            decrypted_content = {
                "body": plaintext,
                "msgtype": "m.text",
            }

        # 构建解密的 RawMatrixEvent，保留原始元数据
        return RawMatrixEvent(
            type=decrypted_content.pop("type", "m.room.message"),
            content=decrypted_content,
            event_id=raw.event_id,
            sender=raw.sender,
            room_id=raw.room_id,
            origin_server_ts=raw.origin_server_ts,
            unsigned=raw.unsigned,
        )
