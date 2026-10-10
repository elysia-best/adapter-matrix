"""Validated device keys and distinct local/cross-signing trust."""

from __future__ import annotations

from typing import Any

from .primitives import OLM_ALGORITHM, verify_json
from .store import CryptoStore
from .types import CryptoError, LocalTrust


def signing_key(value: dict[str, Any], user_id: str, usage: str) -> str:
    keys = value.get("keys", {})
    if (
        value.get("user_id") != user_id
        or value.get("usage") != [usage]
        or len(keys) != 1
    ):
        raise CryptoError("Invalid cross-signing key")
    key_id, public = next(iter(keys.items()))
    if key_id != f"ed25519:{public}":
        raise CryptoError("Cross-signing key ID mismatch")
    return public


class DeviceKeyStore:
    def __init__(self, store: CryptoStore) -> None:
        self.store = store

    def get_device_key(self, user_id: str, device_id: str) -> dict[str, Any] | None:
        return self.store.get("devices", {}).get(user_id, {}).get(device_id)

    def get_device_keys_for_user(self, user_id: str) -> dict[str, Any]:
        return self.store.get("devices", {}).get(user_id, {})

    def identity(self, user_id: str) -> dict[str, Any] | None:
        return self.store.get("identities", {}).get(user_id)

    def is_verified(self, user_id: str, device_id: str) -> bool:
        record = self.get_device_key(user_id, device_id)
        if not record or record.get("local_trust") == LocalTrust.BLACKLISTED:
            return False
        if record.get("local_trust") == LocalTrust.VERIFIED:
            return True
        identity = self.identity(user_id)
        return bool(
            identity
            and identity.get("verified")
            and self.is_cross_signed(user_id, device_id)
        )

    def is_cross_signed(self, user_id: str, device_id: str) -> bool:
        record = self.get_device_key(user_id, device_id)
        identity = self.identity(user_id)
        if not record or not identity or not identity.get("self_signing"):
            return False
        public = signing_key(identity["self_signing"], user_id, "self_signing")
        return verify_json(record["keys"], user_id, public, public)

    def set_trust(self, user_id: str, device_id: str, trust: LocalTrust) -> None:
        devices = self.store.get("devices", {})
        if device_id not in devices.get(user_id, {}):
            raise CryptoError("Unknown device")
        devices[user_id][device_id]["local_trust"] = trust.value
        self.store.put("devices", devices)

    async def query(self, engine: Any, users: list[str]) -> None:
        if not users:
            return
        result = await engine.call("keys_query", device_keys={uid: [] for uid in users})
        devices = self.store.get("devices", {})
        identities = self.store.get("identities", {})
        changes = []
        for user_id in users:
            # A missing/failing homeserver response must not erase cached data.
            if user_id not in result.get("device_keys", {}):
                continue
            old_devices = devices.get(user_id, {})
            updated = {}
            for device_id, keys in result["device_keys"][user_id].items():
                if (
                    not isinstance(keys, dict)
                    or keys.get("user_id") != user_id
                    or keys.get("device_id") != device_id
                ):
                    continue
                public = keys.get("keys", {}).get(f"ed25519:{device_id}")
                if not public or not verify_json(keys, user_id, device_id, public):
                    continue
                old = old_devices.get(device_id)
                # Device IDs must not silently replace either identity key.
                if old and old["keys"]["keys"] != keys["keys"]:
                    changes.append(
                        {"user_id": user_id, "device_id": device_id, "violation": True}
                    )
                    updated[device_id] = {
                        **old,
                        "local_trust": LocalTrust.BLACKLISTED.value,
                    }
                    continue
                updated[device_id] = {
                    "keys": keys,
                    "local_trust": old.get("local_trust", LocalTrust.UNSET.value)
                    if old
                    else LocalTrust.UNSET.value,
                }
            devices[user_id] = updated
            master = result.get("master_keys", {}).get(user_id)
            if master:
                public = signing_key(master, user_id, "master")
                previous = identities.get(user_id, {})
                same = previous.get("master", {}).get("keys") == master["keys"]
                identity = {
                    "master": master,
                    "verified": same and previous.get("verified", False),
                    "previously_verified": previous.get("previously_verified", False)
                    or previous.get("verified", False),
                    "violation": previous.get("violation", False)
                    or (not same and previous.get("verified", False)),
                }
                for usage in ("self_signing", "user_signing"):
                    key = result.get(f"{usage}_keys", {}).get(user_id)
                    if key:
                        signing_key(key, user_id, usage)
                        if not verify_json(key, user_id, public, public):
                            raise CryptoError("Invalid cross-signing signature")
                        identity[usage] = key
                identities[user_id] = identity
                if not same:
                    changes.append(
                        {"user_id": user_id, "violation": identity["violation"]}
                    )
        with self.store.transaction():
            self.store.put("devices", devices)
            self.store.put("identities", identities)
        for change in changes:
            await engine.emit("identity", change)

    async def claim(self, engine: Any, user_id: str, device_id: str) -> str:
        device = self.get_device_key(user_id, device_id)
        if not device or OLM_ALGORITHM not in device["keys"].get("algorithms", []):
            raise CryptoError("Device does not support Olm")
        result = await engine.call(
            "keys_claim", one_time_keys={user_id: {device_id: "signed_curve25519"}}
        )
        public = device["keys"]["keys"][f"ed25519:{device_id}"]
        for key_id, value in (
            result.get("one_time_keys", {}).get(user_id, {}).get(device_id, {}).items()
        ):
            if (
                key_id.startswith("signed_curve25519:")
                and isinstance(value, dict)
                and verify_json(value, user_id, device_id, public)
            ):
                return value["key"]
        raise CryptoError("No valid signed one-time key for device")
