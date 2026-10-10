"""SDK-style device and cross-signing identity handles."""

from __future__ import annotations

from typing import Any

from olm.pk import PkSigning

from .device_keys import signing_key
from .primitives import b64d, b64e, sign_json, verify_json
from .types import CryptoError, LocalTrust


class Device:
    def __init__(self, engine: Any, user_id: str, device_id: str) -> None:
        self.engine, self.user_id, self.device_id = engine, user_id, device_id

    @property
    def keys(self) -> dict[str, Any]:
        record = self.engine.devices.get_device_key(self.user_id, self.device_id)
        if record is None:
            raise CryptoError("Device is no longer present")
        return record["keys"]

    def is_verified(self) -> bool:
        return self.engine.devices.is_verified(self.user_id, self.device_id)

    def is_cross_signed_by_owner(self) -> bool:
        return self.engine.devices.is_cross_signed(self.user_id, self.device_id)

    def is_verified_with_cross_signing(self) -> bool:
        identity = self.engine.devices.identity(self.user_id)
        return bool(
            identity and identity.get("verified") and self.is_cross_signed_by_owner()
        )

    async def set_local_trust(self, trust_state: LocalTrust) -> None:
        self.engine.devices.set_trust(
            self.user_id, self.device_id, LocalTrust(trust_state)
        )
        self.engine.invalidate_outbound_sessions()
        await self.engine.emit(
            "identity", {"user_id": self.user_id, "device_id": self.device_id}
        )

    async def request_verification(self) -> Any:
        return await self.engine.verifications.request(self.user_id, self.device_id)

    async def request_verification_with_methods(self, methods: list[str]) -> Any:
        return await self.engine.verifications.request(
            self.user_id, self.device_id, methods=methods
        )

    async def verify(self) -> None:
        if self.user_id != self.engine.user_id:
            raise CryptoError("Only own devices can be manually cross-signed")
        await self.engine.cross_signing.sign_device(self)
        await self.set_local_trust(LocalTrust.VERIFIED)


class UserDevices:
    def __init__(self, engine: Any, user_id: str) -> None:
        self.engine, self.user_id = engine, user_id

    def keys(self) -> list[str]:
        return list(self.engine.devices.get_device_keys_for_user(self.user_id))

    def get(self, device_id: str) -> Device | None:
        return (
            Device(self.engine, self.user_id, device_id)
            if device_id in self.keys()
            else None
        )

    def devices(self) -> list[Device]:
        return [Device(self.engine, self.user_id, key) for key in self.keys()]


class UserIdentity:
    def __init__(self, engine: Any, user_id: str) -> None:
        self.engine, self.user_id = engine, user_id

    @property
    def data(self) -> dict[str, Any]:
        return self.engine.devices.identity(self.user_id) or {}

    def master_key(self) -> dict[str, Any]:
        return self.data["master"]

    def is_verified(self) -> bool:
        return bool(self.data.get("verified"))

    def was_previously_verified(self) -> bool:
        return bool(self.data.get("previously_verified"))

    def has_verification_violation(self) -> bool:
        return bool(self.data.get("violation"))

    async def verify(self) -> None:
        await self.engine.cross_signing.sign_user(self.user_id)
        await self.engine.cross_signing.trust_identity(self.user_id)

    async def withdraw_verification(self) -> None:
        identities = self.engine.store.get("identities", {})
        identities[self.user_id]["verified"] = False
        identities[self.user_id]["violation"] = False
        self.engine.store.put("identities", identities)
        self.engine.invalidate_outbound_sessions()
        await self.engine.emit("identity", {"user_id": self.user_id})

    async def request_verification(self, room_id: str | None = None) -> Any:
        if self.user_id == self.engine.user_id:
            return await self.engine.verifications.request(self.user_id, "*")
        if room_id is None:
            raise ValueError("Cross-user verification requires a shared room_id")
        return await self.engine.verifications.request(
            self.user_id, "*", room_id=room_id
        )


class CrossSigning:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def status(self) -> dict[str, bool]:
        return {
            f"has_{usage}": self.engine.store.get(f"secret/m.cross_signing.{usage}")
            is not None
            for usage in ("master", "self_signing", "user_signing")
        }

    def signer(self, usage: str) -> PkSigning:
        value = self.engine.store.get(f"secret/m.cross_signing.{usage}")
        if value is None:
            raise CryptoError(f"Missing cross-signing {usage} secret")
        return PkSigning(b64d(value))

    async def bootstrap(
        self, auth_data: dict[str, Any] | None = None, *, reset: bool = False
    ) -> None:
        engine = self.engine
        await engine.devices.query(engine, [engine.user_id])
        existing = engine.devices.identity(engine.user_id)
        pending = engine.store.get("cross_signing_pending")
        if existing and not reset and not pending:
            if not all(self.status().values()):
                raise CryptoError(
                    "Recover the existing cross-signing secrets before bootstrapping"
                )
            await self.sign_device(Device(engine, engine.user_id, engine.device_id))
            return
        if pending is None:
            seeds = {
                usage: b64e(PkSigning.generate_seed())
                for usage in ("master", "self_signing", "user_signing")
            }
            signers = {usage: PkSigning(b64d(seed)) for usage, seed in seeds.items()}
            keys = {
                usage: {
                    "user_id": engine.user_id,
                    "usage": [usage],
                    "keys": {f"ed25519:{signer.public_key}": signer.public_key},
                }
                for usage, signer in signers.items()
            }
            master = signers["master"]
            for usage in ("self_signing", "user_signing"):
                sign_json(keys[usage], master, engine.user_id, master.public_key)
            sign_json(
                keys["master"], engine.account.account, engine.user_id, engine.device_id
            )
            pending = {"seeds": seeds, "keys": keys}
            engine.store.put("cross_signing_pending", pending)
        body = {f"{usage}_key": key for usage, key in pending["keys"].items()}
        if auth_data is not None:
            body["auth"] = auth_data
        await engine.call("keys_device_signing_upload", **body)
        with engine.store.transaction():
            for usage, seed in pending["seeds"].items():
                engine.store.put(f"secret/m.cross_signing.{usage}", seed)
            engine.store.delete("cross_signing_pending")
        await engine.devices.query(engine, [engine.user_id])
        await self.trust_identity(engine.user_id)
        await self.sign_device(Device(engine, engine.user_id, engine.device_id))

    async def import_secret(self, name: str, seed: str) -> None:
        usage = name.removeprefix("m.cross_signing.")
        if usage not in {"master", "self_signing", "user_signing"}:
            raise ValueError("Unknown cross-signing secret")
        await self.engine.devices.query(self.engine, [self.engine.user_id])
        identity = self.engine.devices.identity(self.engine.user_id)
        if identity is None or usage not in identity:
            raise CryptoError("Published cross-signing key is unavailable")
        signer = PkSigning(b64d(seed))
        if signer.public_key != signing_key(
            identity[usage], self.engine.user_id, usage
        ):
            raise CryptoError("Secret does not match the published cross-signing key")
        self.engine.store.put(f"secret/{name}", seed)
        if usage == "master":
            await self.trust_identity(self.engine.user_id)

    async def trust_identity(self, user_id: str) -> None:
        identities = self.engine.store.get("identities", {})
        if user_id not in identities:
            return
        identities[user_id].update(
            verified=True, previously_verified=True, violation=False
        )
        self.engine.store.put("identities", identities)
        await self.engine.emit("identity", {"user_id": user_id})

    async def sign_device(self, device: Device) -> None:
        signer = self.signer("self_signing")
        keys = sign_json(device.keys, signer, self.engine.user_id, signer.public_key)
        result = await self.engine.request(
            "keys_signatures_upload",
            {"signatures": {device.user_id: {device.device_id: keys}}},
        )
        if result.get("failures"):
            raise CryptoError("Device signature upload failed")
        await self.engine.devices.query(self.engine, [device.user_id])

    async def sign_user(self, user_id: str) -> None:
        identity = self.engine.devices.identity(user_id)
        if identity is None:
            raise CryptoError("Unknown cross-signing identity")
        if user_id == self.engine.user_id:
            return
        master = identity["master"]
        public = signing_key(master, user_id, "master")
        signer = self.signer("user_signing")
        signed = sign_json(master, signer, self.engine.user_id, signer.public_key)
        result = await self.engine.request(
            "keys_signatures_upload", {"signatures": {user_id: {public: signed}}}
        )
        if result.get("failures"):
            raise CryptoError("User signature upload failed")

    async def refresh_trust(self) -> None:
        identities = self.engine.store.get("identities", {})
        own = identities.get(self.engine.user_id)
        if not own or not own.get("verified") or not own.get("user_signing"):
            return
        key = signing_key(own["user_signing"], self.engine.user_id, "user_signing")
        for user_id, identity in identities.items():
            if user_id != self.engine.user_id and verify_json(
                identity["master"], self.engine.user_id, key, key
            ):
                identity.update(
                    verified=True, previously_verified=True, violation=False
                )
        self.engine.store.put("identities", identities)
