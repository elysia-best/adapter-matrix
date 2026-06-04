"""DeviceKeyStore — 设备密钥缓存和查询。

缓存其他用户的设备密钥信息，并通过 /keys/query 和 /keys/claim API
获取和更新密钥。当服务端通知设备列表变更时，自动查询新设备的密钥。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .store import CryptoStore
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


class DeviceKeyStore:
    """缓存和维护 Matrix 设备密钥信息。

    密钥缓存结构:
        {user_id: {device_id: {keys, algorithms, signatures, ...}}}

    同时维护一个待查询用户队列，在下一轮同步中批量查询。
    """

    def __init__(self, store: CryptoStore) -> None:
        self._store = store
        self._keys: dict[str, dict[str, dict[str, Any]]] = {}
        self._pending_query: set[str] = set()

    def load(self) -> None:
        """从持久化存储加载设备密钥缓存。"""
        self._keys = self._store.load_device_keys()
        log("TRACE", f"已加载 {sum(len(d) for d in self._keys.values())} 个设备密钥")

    def _save(self) -> None:
        self._store.save_device_keys(self._keys)

    def mark_for_query(self, user_ids: list[str]) -> None:
        """标记用户 ID 列表，在下次同步时查询其设备密钥。"""
        self._pending_query.update(user_ids)

    def mark_left(self, user_ids: list[str]) -> None:
        """用户离开后清理其设备密钥缓存。"""
        for uid in user_ids:
            self._keys.pop(uid, None)
        self._save()

    def get_device_keys_for_user(self, user_id: str) -> dict[str, dict[str, Any]]:
        """获取指定用户的所有已知设备密钥。"""
        return self._keys.get(user_id, {})

    def get_device_key(self, user_id: str, device_id: str) -> dict[str, Any] | None:
        """获取指定设备的密钥信息。"""
        return self._keys.get(user_id, {}).get(device_id)

    def get_device_curve25519_key(self, user_id: str, device_id: str) -> str | None:
        """获取指定设备的 Curve25519 身份密钥。"""
        device = self.get_device_key(user_id, device_id)
        if device is None:
            return None
        keys = device.get("keys", {})
        return keys.get(f"curve25519:{device_id}")

    def get_device_ed25519_key(self, user_id: str, device_id: str) -> str | None:
        """获取指定设备的 Ed25519 签名密钥。"""
        device = self.get_device_key(user_id, device_id)
        if device is None:
            return None
        keys = device.get("keys", {})
        return keys.get(f"ed25519:{device_id}")

    async def query_keys(self, adapter: Adapter, bot: Bot) -> None:
        """批量查询待查询用户的设备密钥。

        调用 /keys/query API，获取所有设备的身份和签名密钥，
        并更新本地缓存。
        """
        if not self._pending_query:
            return

        user_ids = list(self._pending_query)
        self._pending_query.clear()
        log("TRACE", f"查询 {len(user_ids)} 个用户的设备密钥")

        # batch: {user_id: []} 表示查询所有设备
        batch = {uid: [] for uid in user_ids}
        result = await adapter._api_keys_query(bot, device_keys=batch)  # type: ignore[union-attr]

        for user_id, devices in result.get("device_keys", {}).items():
            self._keys.setdefault(user_id, {})
            for device_id, key_data in devices.items():
                if key_data is None:
                    # 设备已删除
                    self._keys[user_id].pop(device_id, None)
                else:
                    self._keys[user_id][device_id] = key_data

        self._save()
        log(
            "TRACE",
            f"已更新设备密钥缓存，当前 {sum(len(d) for d in self._keys.values())} 个设备",
        )

    async def claim_one_time_key(
        self, adapter: Adapter, bot: Bot, user_id: str, device_id: str
    ) -> str | None:
        """为指定设备索取一次性 Curve25519 密钥。

        调用 /keys/claim API，返回 signed_curve25519 类型的 OTK 值。
        此密钥用于建立出站 Olm 会话。

        Returns:
            one_time_key 的 base64 值，如果索取失败则返回 None
        """
        result = await adapter._api_keys_claim(  # type: ignore[union-attr]
            bot,
            one_time_keys={user_id: {device_id: "signed_curve25519"}},
        )

        failures = result.get("failures", {})
        if failures and user_id in failures:
            log("TRACE", f"索取 {user_id}/{device_id} 的 OTK 失败")
            return None

        one_time_keys = result.get("one_time_keys", {})
        device_keys = one_time_keys.get(user_id, {}).get(device_id, {})
        if not device_keys:
            return None

        for key_data in device_keys.values():
            if isinstance(key_data, dict):
                return key_data.get("key")
            return key_data

        return None
