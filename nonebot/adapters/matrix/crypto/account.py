"""Persistent upstream libolm account and signed key publication."""

from __future__ import annotations

from typing import Any

from olm.account import Account

from .primitives import MEGOLM_ALGORITHM, OLM_ALGORITHM, sign_json
from .store import CryptoStore
from .types import CryptoError, StoreError


class OlmAccountManager:
    def __init__(self, store: CryptoStore) -> None:
        self._store = store
        self._account: Account | None = None

    @property
    def account(self) -> Account:
        if self._account is None:
            raise CryptoError("Olm account is not initialized")
        return self._account

    def load_or_create(self) -> None:
        self._account = self._store.load_account()
        if self._account is None:
            if not self._store.new_database:
                raise StoreError(
                    "Account is missing from the crypto store; use a new device and directory"
                )
            self._account = Account()
            self.save()

    def save(self) -> None:
        self._store.save_account(self.account)

    def build_device_keys(self, bot: Any) -> dict[str, Any]:
        if not bot.device_id:
            raise CryptoError(
                "E2EE requires a device ID associated with the access token"
            )
        keys = self.account.identity_keys
        return sign_json(
            {
                "user_id": str(bot.user_id),
                "device_id": bot.device_id,
                "algorithms": [OLM_ALGORITHM, MEGOLM_ALGORITHM],
                "keys": {f"{kind}:{bot.device_id}": key for kind, key in keys.items()},
            },
            self.account,
            str(bot.user_id),
            bot.device_id,
        )

    async def replenish(
        self, engine: Any, counts: dict[str, int], unused_fallback: list[str] | None
    ) -> None:
        account = self.account
        body: dict[str, Any] = {}
        # Unpublished keys are retried, never replaced on transport failure.
        target = account.max_one_time_keys // 2
        pending = len(account.one_time_keys.get("curve25519", {}))
        needed = max(0, target - counts.get("signed_curve25519", 0) - pending)
        if needed:
            account.generate_one_time_keys(needed)
        body["one_time_keys"] = self._signed_keys(engine, account.one_time_keys)
        fallback_exists = self._store.get("fallback_published", False)
        if not fallback_exists or (
            unused_fallback is not None and "signed_curve25519" not in unused_fallback
        ):
            if not account.fallback_key.get("curve25519"):
                account.generate_fallback_key()
            body["fallback_keys"] = self._signed_keys(
                engine, account.fallback_key, fallback=True
            )
        body = {key: value for key, value in body.items() if value}
        if not body:
            return
        self.save()
        await engine.request("keys_upload", body, purpose="publish_keys")

    def published(self, body: dict[str, Any]) -> None:
        self.account.mark_keys_as_published()
        with self._store.transaction():
            if body.get("fallback_keys"):
                self._store.put("fallback_published", True)
            self.save()

    def _signed_keys(
        self, engine: Any, keys: dict[str, Any], *, fallback: bool = False
    ) -> dict[str, Any]:
        result = {}
        for key_id, key in keys.get("curve25519", {}).items():
            value: dict[str, Any] = {"key": key}
            if fallback:
                value["fallback"] = True
            result[f"signed_curve25519:{key_id}"] = sign_json(
                value, self.account, engine.user_id, engine.device_id
            )
        return result
