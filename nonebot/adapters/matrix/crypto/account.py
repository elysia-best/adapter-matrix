"""OlmAccountManager — 管理设备的 Olm 身份密钥、一次性密钥和 fallback 密钥。

每个 Matrix 设备拥有一对 Ed25519（签名）和 Curve25519（身份）密钥，
以及一组一次性 Curve25519 密钥用于 Olm 会话建立。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import olm

from .store import CryptoStore
from ..serialization import encode_json_text
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


class OlmAccountManager:
    """管理 OlmAccount 的生命周期和密钥上传。"""

    def __init__(self, store: CryptoStore) -> None:
        self._store = store
        self._account: olm.Account | None = None

    @property
    def account(self) -> olm.Account:
        if self._account is None:
            msg = "OlmAccount 尚未初始化"
            raise RuntimeError(msg)
        return self._account

    def load_or_create(self) -> None:
        """从持久化存储加载 OlmAccount，如果不存在则创建新的。

        新创建的账户包含随机生成的 Ed25519 和 Curve25519 密钥对。
        """
        existing = self._store.load_account()
        if existing is not None:
            self._account = existing
            log("TRACE", "已从存储加载 OlmAccount")
        else:
            self._account = olm.Account()
            log("INFO", "已创建新的 OlmAccount")
            self._save()

    async def upload_identity_keys(
        self, adapter: Adapter, bot: Bot
    ) -> dict[str, object]:
        """上传 Ed25519 和 Curve25519 身份密钥到 homeserver。

        使用 Ed25519 密钥对 device_keys JSON 进行自签名，
        符合 Matrix 规范中的签名要求。
        """
        account = self.account
        identity_keys = account.identity_keys
        device_id = bot.device_id
        if device_id is None:
            msg = "设备 ID 为空，无法上传身份密钥"
            raise RuntimeError(msg)

        device_keys: dict[str, object] = {
            "user_id": str(bot.user_id),
            "device_id": device_id,
            "algorithms": [
                "m.megolm.v1.aes-sha2",
                "m.olm.v1.curve25519-aes-sha2",
            ],
            "keys": {
                f"ed25519:{device_id}": identity_keys["ed25519"],
                f"curve25519:{device_id}": identity_keys["curve25519"],
            },
            "signatures": {},
        }
        signature = account.sign(encode_json_text(device_keys).encode("utf-8"))
        device_keys["signatures"] = {
            str(bot.user_id): {
                f"ed25519:{device_id}": signature,
            },
        }

        result = await adapter._api_keys_upload(bot, device_keys=device_keys)
        counts = result.get("one_time_key_counts", {})
        log(
            "INFO",
            f"已上传设备身份密钥，OTK 计数: "
            f"signed_curve25519={counts.get('signed_curve25519', 0)}",
        )
        return result

    async def upload_one_time_keys(
        self, adapter: Adapter, bot: Bot, *, count: int = 50
    ) -> None:
        """生成并上传一次性 Curve25519 密钥。

        Olm 会话建立时，每个设备需要消耗一个 OTK。
        应在密钥数量不足时补充。
        """
        account = self.account
        account.generate_one_time_keys(count)
        otks = account.one_time_keys
        formatted: dict[str, str] = {}
        for key_id, key in otks.get("curve25519", {}).items():
            formatted[f"signed_curve25519:{key_id}"] = key

        if not formatted:
            log("TRACE", "没有新的一次性密钥需要上传")
            return

        await adapter._api_keys_upload(bot, one_time_keys=formatted)
        account.mark_keys_as_published()
        self._save()
        log("TRACE", f"已上传 {len(formatted)} 个一次性密钥")

    async def upload_fallback_key(self, adapter: Adapter, bot: Bot) -> None:
        """生成并上传 fallback 密钥。

        当服务端的一次性密钥耗尽时，fallback 密钥作为备用。
        每次上传会覆盖之前的 fallback 密钥。
        """
        account = self.account
        account.generate_fallback_key()
        fallback = account.fallback_key
        formatted: dict[str, str] = {}
        if "curve25519" in fallback:
            for key_id, key in fallback["curve25519"].items():
                formatted[f"signed_curve25519:{key_id}"] = key

        if formatted:
            await adapter._api_keys_upload(bot, fallback_keys=formatted)
            account.mark_keys_as_published()
            self._save()
            log("TRACE", "已上传 fallback 密钥")

    async def ensure_one_time_keys(
        self, adapter: Adapter, bot: Bot, *, threshold: int = 10, count: int = 50
    ) -> None:
        """检查一次性密钥数量，不足时自动补充。

        服务端会跟踪剩余 OTK 数量（通过 one_time_key_counts API 返回），
        当数量低于 threshold 时，生成并上传新密钥。
        """
        max_keys = self.account.max_one_time_keys
        if max_keys < threshold:
            await self.upload_one_time_keys(adapter, bot, count=count)

    def _save(self) -> None:
        self._store.save_account(self.account)
