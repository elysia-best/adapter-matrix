"""OlmAccountManager — 管理设备的 Olm 身份密钥、一次性密钥和 fallback 密钥。

每个 Matrix 设备拥有一对 Ed25519（签名）和 Curve25519（身份）密钥，
以及一组一次性 Curve25519 密钥用于 Olm 会话建立。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import olm

from .store import CryptoStore
from ..serialization import encode_matrix_canonical_json
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

    def build_device_keys(self, bot: Bot) -> dict[str, object]:
        """构建当前设备的已签名 device_keys 结构。"""
        account = self.account
        identity_keys = account.identity_keys
        device_id = self._require_device_id(bot)

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
        }
        signature = account.sign(
            encode_matrix_canonical_json(device_keys).encode("utf-8")
        )
        device_keys["signatures"] = {
            str(bot.user_id): {
                f"ed25519:{device_id}": signature,
            },
        }
        return device_keys

    async def upload_identity_keys(
        self, adapter: Adapter, bot: Bot
    ) -> dict[str, object]:
        """上传 Ed25519 和 Curve25519 身份密钥到 homeserver。

        使用 Ed25519 密钥对 device_keys JSON 进行自签名，
        符合 Matrix 规范中的签名要求。
        """
        device_keys = self.build_device_keys(bot)

        result = await adapter._api_keys_upload(bot, device_keys=device_keys)
        counts = self._extract_otk_counts(result)
        log(
            "INFO",
            f"已上传设备身份密钥，OTK 计数: "
            f"signed_curve25519={counts.get('signed_curve25519', 0)}",
        )
        return result

    async def upload_one_time_keys(
        self, adapter: Adapter, bot: Bot, *, count: int = 50
    ) -> dict[str, object]:
        """生成并上传一次性 Curve25519 密钥。

        Olm 会话建立时，每个设备需要消耗一个 OTK。
        应在密钥数量不足时补充。
        """
        account = self.account
        device_id = self._require_device_id(bot)
        account.generate_one_time_keys(count)
        otks = account.one_time_keys
        formatted: dict[str, dict[str, object]] = {}
        for key_id, key in otks.get("curve25519", {}).items():
            formatted[f"signed_curve25519:{key_id}"] = (
                self._build_signed_curve25519_key(
                    bot,
                    device_id=device_id,
                    key=key,
                )
            )

        if not formatted:
            log("TRACE", "没有新的一次性密钥需要上传")
            return {}

        result = await adapter._api_keys_upload(bot, one_time_keys=formatted)
        account.mark_keys_as_published()
        self._save()
        counts = self._extract_otk_counts(result)
        log(
            "INFO",
            f"已上传 {len(formatted)} 个一次性密钥，"
            f"服务端剩余 signed_curve25519={counts.get('signed_curve25519', 0)}",
        )
        return result

    async def upload_fallback_key(
        self, adapter: Adapter, bot: Bot
    ) -> dict[str, object]:
        """生成并上传 fallback 密钥。

        当服务端的一次性密钥耗尽时，fallback 密钥作为备用。
        每次上传会覆盖之前的 fallback 密钥。
        """
        account = self.account
        device_id = self._require_device_id(bot)
        account.generate_fallback_key()
        fallback = account.fallback_key
        formatted: dict[str, dict[str, object]] = {}
        if "curve25519" in fallback:
            for key_id, key in fallback["curve25519"].items():
                formatted[f"signed_curve25519:{key_id}"] = (
                    self._build_signed_curve25519_key(
                        bot,
                        device_id=device_id,
                        key=key,
                        fallback=True,
                    )
                )

        if not formatted:
            return {}

        result = await adapter._api_keys_upload(bot, fallback_keys=formatted)
        account.mark_keys_as_published()
        self._save()
        counts = self._extract_otk_counts(result)
        log(
            "TRACE",
            f"已上传 fallback 密钥，"
            f"服务端剩余 signed_curve25519={counts.get('signed_curve25519', 0)}",
        )
        return result

    async def ensure_one_time_keys(
        self,
        adapter: Adapter,
        bot: Bot,
        *,
        key_upload_response: dict[str, object] | None = None,
        threshold: int = 10,
        count: int = 50,
    ) -> dict[str, object]:
        """检查一次性密钥数量，不足时自动补充。

        服务端会跟踪剩余 OTK 数量（通过 one_time_key_counts API 返回），
        当数量低于 threshold 时，生成并上传新密钥。
        """
        counts = self._extract_otk_counts(key_upload_response or {})
        current = counts.get("signed_curve25519", 0)
        if current >= threshold:
            log("INFO", f"服务端 signed_curve25519 OTK 数量充足: {current}，无需补充")
            return {}

        upload_count = max(count - current, threshold - current, 1)
        log(
            "INFO",
            f"服务端 signed_curve25519 OTK 数量不足: {current}，"
            f"准备上传 {upload_count} 个一次性密钥",
        )
        return await self.upload_one_time_keys(adapter, bot, count=upload_count)

    def _build_signed_curve25519_key(
        self,
        bot: Bot,
        *,
        device_id: str,
        key: str,
        fallback: bool = False,
    ) -> dict[str, object]:
        key_object: dict[str, object] = {"key": key}
        if fallback:
            key_object["fallback"] = True
        signature = self.account.sign(
            encode_matrix_canonical_json(key_object).encode("utf-8")
        )
        key_object["signatures"] = {
            str(bot.user_id): {
                f"ed25519:{device_id}": signature,
            },
        }
        return key_object

    @staticmethod
    def _extract_otk_counts(result: dict[str, object]) -> dict[str, int]:
        counts = result.get("one_time_key_counts")
        if not isinstance(counts, dict):
            return {}
        return {
            key: value
            for key, value in counts.items()
            if isinstance(key, str) and isinstance(value, int)
        }

    @staticmethod
    def _require_device_id(bot: Bot) -> str:
        device_id = bot.device_id
        if device_id is None:
            msg = "设备 ID 为空，无法上传加密密钥"
            raise RuntimeError(msg)
        return device_id

    def _save(self) -> None:
        self._store.save_account(self.account)
