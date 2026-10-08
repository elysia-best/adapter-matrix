"""Device identity and one-time key management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .primitives import IdentityAccount, canonical_json
from .store import CryptoStore
from ..serialization import encode_matrix_canonical_json
from ..utils import log

if TYPE_CHECKING:
    from ..adapter import Adapter
    from ..bot import Bot


class OlmAccountManager:
    """Compatibility name for the pure Python device account manager."""

    def __init__(self, store: CryptoStore) -> None:
        self._store = store
        self._account: IdentityAccount | None = None

    @property
    def account(self) -> IdentityAccount:
        if self._account is None:
            msg = "crypto account has not been initialised"
            raise RuntimeError(msg)
        return self._account

    def load_or_create(self) -> None:
        self._account = self._store.load_account() or IdentityAccount.create()
        self._save()

    def build_device_keys(self, bot: Bot) -> dict[str, object]:
        device_id = self._require_device_id(bot)
        identity = self.account.identity_keys
        device_keys: dict[str, object] = {
            "user_id": str(bot.user_id),
            "device_id": device_id,
            "algorithms": [
                "m.megolm.v1.aes-sha2",
                "m.olm.v1.curve25519-aes-sha2",
            ],
            "keys": {
                f"ed25519:{device_id}": identity["ed25519"],
                f"curve25519:{device_id}": identity["curve25519"],
            },
        }
        signature = self.account.sign(
            encode_matrix_canonical_json(device_keys).encode()
        )
        device_keys["signatures"] = {
            str(bot.user_id): {f"ed25519:{device_id}": signature}
        }
        return device_keys

    async def upload_identity_keys(
        self, adapter: Adapter, bot: Bot
    ) -> dict[str, object]:
        result = await adapter._api_keys_upload(
            bot, device_keys=self.build_device_keys(bot)
        )
        log("INFO", "uploaded Matrix device identity keys")
        return result

    async def upload_one_time_keys(
        self, adapter: Adapter, bot: Bot, *, count: int = 50
    ) -> dict[str, object]:
        self.account.generate_one_time_keys(count)
        device_id = self._require_device_id(bot)
        formatted: dict[str, dict[str, object]] = {}
        for key_id, key in self.account.one_time_keys.get("curve25519", {}).items():
            body: dict[str, object] = {"key": key}
            body["signatures"] = {
                str(bot.user_id): {
                    f"ed25519:{device_id}": self.account.sign(canonical_json(body))
                }
            }
            formatted[f"signed_curve25519:{key_id}"] = body
        if not formatted:
            return {}
        result = await adapter._api_keys_upload(bot, one_time_keys=formatted)
        self.account.mark_keys_as_published()
        self._save()
        return result

    async def upload_fallback_key(
        self, adapter: Adapter, bot: Bot
    ) -> dict[str, object]:
        if self.account.fallback_private:
            return {}
        self.account.generate_fallback_key()
        device_id = self._require_device_id(bot)
        formatted: dict[str, dict[str, object]] = {}
        for key_id, key in self.account.fallback_key.get("curve25519", {}).items():
            body: dict[str, object] = {"key": key, "fallback": True}
            body["signatures"] = {
                str(bot.user_id): {
                    f"ed25519:{device_id}": self.account.sign(canonical_json(body))
                }
            }
            formatted[f"signed_curve25519:{key_id}"] = body
        result = await adapter._api_keys_upload(bot, fallback_keys=formatted)
        self._save()
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
        counts = self._extract_otk_counts(key_upload_response or {})
        current = counts.get("signed_curve25519", 0)
        if current >= threshold:
            return {}
        return await self.upload_one_time_keys(
            adapter, bot, count=max(count - current, threshold - current, 1)
        )

    @staticmethod
    def _extract_otk_counts(result: dict[str, object]) -> dict[str, int]:
        counts = result.get("one_time_key_counts")
        return (
            {
                str(key): value
                for key, value in counts.items()
                if isinstance(counts, dict)
                and isinstance(key, str)
                and isinstance(value, int)
            }
            if isinstance(counts, dict)
            else {}
        )

    @staticmethod
    def _require_device_id(bot: Bot) -> str:
        if not bot.device_id:
            msg = "device ID is required for Matrix E2EE"
            raise RuntimeError(msg)
        return bot.device_id

    def _save(self) -> None:
        self._store.save_account(self.account)
