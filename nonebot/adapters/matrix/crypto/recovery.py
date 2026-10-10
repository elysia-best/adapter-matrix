"""Cross-signing recovery and authenticated server-side key backups."""

from __future__ import annotations

import json
import os
from typing import Any

import olm

from .device_keys import signing_key
from .native import pk_from_private
from .primitives import MEGOLM_ALGORITHM, b64d, b64e, sign_json, verify_json
from .secret_storage import SECRETS, SecretStorage
from .types import BackupState, CryptoError, Observable, RecoveryState
from ..exception import ActionFailed, InteractiveAuthRequired

BACKUP_ALGORITHM = "m.megolm_backup.v1.curve25519-aes-sha2"


class Backups:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._state = Observable(BackupState.UNKNOWN)

    def state(self) -> BackupState:
        return self._state.value

    def state_stream(self) -> Any:
        return self._state.changes()

    async def _change(self, state: BackupState, **progress: Any) -> None:
        self._state.set(state)
        await self.engine.emit("backup", {"state": state.value, **progress})

    async def fetch_exists_on_server(self) -> bool:
        return await self._version() is not None

    async def exists_on_server(self) -> bool:
        return await self.fetch_exists_on_server()

    async def _version(self) -> dict[str, Any] | None:
        try:
            return await self.engine.call("room_keys_version")
        except ActionFailed as exc:
            if exc.status_code == 404:
                return None
            raise

    async def are_enabled(self) -> bool:
        return self.engine.store.get("backup") is not None

    async def enable(self, private_key: str | None = None) -> None:
        await self._change(BackupState.ENABLING)
        version = await self._version()
        if not version or version.get("algorithm") != BACKUP_ALGORITHM:
            raise CryptoError("No supported backup is available")
        auth = version["auth_data"]
        if private_key is not None:
            decryptor = pk_from_private(b64d(private_key))
            if decryptor.public_key != auth.get("public_key"):
                raise CryptoError(
                    "Backup private key does not match the current backup"
                )
            self.engine.store.put("secret/m.megolm_backup.v1", private_key)
        else:
            identity = self.engine.devices.identity(self.engine.user_id)
            trusted = False
            if identity and identity.get("verified"):
                key = signing_key(identity["master"], self.engine.user_id, "master")
                trusted = verify_json(auth, self.engine.user_id, key, key)
            for device_id, record in self.engine.devices.get_device_keys_for_user(
                self.engine.user_id
            ).items():
                if self.engine.devices.is_verified(self.engine.user_id, device_id):
                    key = record["keys"]["keys"][f"ed25519:{device_id}"]
                    trusted |= verify_json(auth, self.engine.user_id, device_id, key)
            if not trusted:
                raise CryptoError("Backup has no trusted signature")
        self.engine.store.put("backup", version)
        await self._change(BackupState.ENABLED)

    async def create(self) -> None:
        if await self.fetch_exists_on_server():
            raise CryptoError(
                "An existing backup must be recovered or explicitly deleted first"
            )
        await self._change(BackupState.CREATING)
        secret = b64e(os.urandom(32))
        decryptor = pk_from_private(b64d(secret))
        auth = sign_json(
            {"public_key": decryptor.public_key},
            self.engine.account.account,
            self.engine.user_id,
            self.engine.device_id,
        )
        if self.engine.cross_signing.status()["has_master"]:
            signer = self.engine.cross_signing.signer("master")
            sign_json(auth, signer, self.engine.user_id, signer.public_key)
        self.engine.store.put("backup_pending", {"secret": secret, "auth_data": auth})
        result = await self.engine.call(
            "room_keys_create_version", algorithm=BACKUP_ALGORITHM, auth_data=auth
        )
        with self.engine.store.transaction():
            self.engine.store.put("secret/m.megolm_backup.v1", secret)
            self.engine.store.put(
                "backup",
                {
                    "version": result["version"],
                    "algorithm": BACKUP_ALGORITHM,
                    "auth_data": auth,
                },
            )
            self.engine.store.delete("backup_pending")
        await self._change(BackupState.ENABLED)

    async def upload(self) -> int:
        backup = self.engine.store.get("backup")
        if not backup:
            return 0
        current = await self._version()
        if (
            not current
            or current["version"] != backup["version"]
            or current["auth_data"]["public_key"] != backup["auth_data"]["public_key"]
        ):
            self.engine.store.delete("backup")
            await self._change(BackupState.UNKNOWN)
            raise CryptoError(
                "Backup version changed; validate the new backup before uploading"
            )
        encryptor = olm.PkEncryption(backup["auth_data"]["public_key"])
        rooms: dict[str, Any] = {}
        names = []
        for value in self.engine.megolm.export_keys():
            name = f"inbound/{value['room_id']}/{value['session_id']}"
            record = self.engine.store.get(name)
            if record.get("backed_up") == backup["version"]:
                continue
            payload = {
                key: value[key]
                for key in (
                    "algorithm",
                    "sender_key",
                    "sender_claimed_keys",
                    "forwarding_curve25519_key_chain",
                    "session_key",
                )
            }
            encrypted = encryptor.encrypt(json.dumps(payload, separators=(",", ":")))
            rooms.setdefault(value["room_id"], {"sessions": {}})["sessions"][
                value["session_id"]
            ] = {
                "first_message_index": value["first_known_index"],
                "forwarded_count": len(
                    value.get("forwarding_curve25519_key_chain", [])
                ),
                "is_verified": bool(
                    record.get("sender") and self.engine.is_sender_trusted(record)
                ),
                "session_data": {
                    "ephemeral": encrypted.ephemeral_key,
                    "mac": encrypted.mac,
                    "ciphertext": encrypted.ciphertext,
                },
            }
            names.append(name)
        if rooms:
            await self.engine.request(
                "room_keys_put_keys",
                {"version": backup["version"], "rooms": rooms},
                purpose="backup_upload",
                metadata={"names": names, "version": backup["version"]},
            )
        await self._change(BackupState.ENABLED, uploaded=len(names))
        return len(names)

    async def wait_for_steady_state(self) -> None:
        await self.upload()

    async def download(
        self, room_id: str | None = None, session_id: str | None = None
    ) -> int:
        backup = self.engine.store.get("backup")
        secret = self.engine.store.get("secret/m.megolm_backup.v1")
        if not backup or not secret:
            raise CryptoError("A validated backup and its private key are required")
        decryptor = pk_from_private(b64d(secret))
        if decryptor.public_key != backup["auth_data"]["public_key"]:
            raise CryptoError("Backup key mismatch")
        await self._change(BackupState.DOWNLOADING)
        data = await self.engine.call(
            "room_keys_keys",
            version=backup["version"],
            room_id=room_id,
            session_id=session_id,
        )
        rooms = (
            data.get("rooms", {})
            if room_id is None
            else {room_id: {"sessions": {session_id: data}} if session_id else data}
        )
        count = 0
        for room, room_data in rooms.items():
            for sid, record in room_data.get("sessions", {}).items():
                encrypted = record["session_data"]
                message = olm.PkMessage(
                    encrypted["ephemeral"], encrypted["mac"], encrypted["ciphertext"]
                )
                value = json.loads(decryptor.decrypt(message, unicode_errors="strict"))
                if value.get("algorithm") != MEGOLM_ALGORITHM:
                    raise CryptoError("Unsupported backup room key")
                value.update(room_id=room, session_id=sid)
                count += self.engine.megolm.import_key(value)
        await self._change(BackupState.ENABLED, downloaded=count)
        await self.engine.retry_decryption()
        return count

    async def download_room_keys_for_room(self, room_id: str) -> int:
        return await self.download(room_id)

    async def download_room_key(self, room_id: str, session_id: str) -> int:
        return await self.download(room_id, session_id)

    async def disable(self) -> None:
        self.engine.store.delete("backup")
        await self._change(BackupState.UNKNOWN)

    async def disable_and_delete(self) -> None:
        backup = self.engine.store.get("backup") or await self._version()
        if backup:
            await self.engine.call(
                "room_keys_delete_version", version=backup["version"]
            )
        await self.disable()


class IdentityResetHandle:
    def __init__(self, engine: Any, challenge: InteractiveAuthRequired) -> None:
        self.engine, self.challenge = engine, challenge
        self.cancelled = False

    def auth_type(self) -> dict[str, Any]:
        return self.challenge.body or {}

    async def reset(self, auth: dict[str, Any] | None = None) -> None:
        if self.cancelled:
            raise CryptoError("Identity reset was cancelled")
        await self.engine.cross_signing.bootstrap(auth, reset=True)

    async def auth(self, auth: dict[str, Any] | None = None) -> None:
        await self.reset(auth)

    async def cancel(self) -> None:
        self.cancelled = True
        self.engine.store.delete("cross_signing_pending")


class Recovery:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._state = Observable(RecoveryState.UNKNOWN)

    def state(self) -> RecoveryState:
        return self._state.value

    def state_stream(self) -> Any:
        return self._state.changes()

    async def refresh(self) -> RecoveryState:
        enabled = await SecretStorage(self.engine).is_enabled()
        complete = all(
            self.engine.store.get(f"secret/{name}") is not None for name in SECRETS
        )
        state = (
            RecoveryState.ENABLED
            if enabled and complete
            else RecoveryState.INCOMPLETE
            if enabled
            else RecoveryState.DISABLED
        )
        self._state.set(state)
        await self.engine.emit("recovery", {"state": state.value})
        return state

    async def enable(
        self, *, passphrase: str | None = None, wait_for_backups_to_upload: bool = False
    ) -> str:
        await self.engine.cross_signing.bootstrap()
        await self.enable_backup()
        store = await SecretStorage(self.engine).create_secret_store(
            passphrase=passphrase
        )
        if wait_for_backups_to_upload:
            await self.engine.backups().upload()
        await self.refresh()
        # Return the secret to the explicit caller, never put it in general events.
        return store.secret_storage_key()

    async def enable_backup(self) -> None:
        backups = self.engine.backups()
        if await backups.fetch_exists_on_server():
            await backups.enable(self.engine.store.get("secret/m.megolm_backup.v1"))
        else:
            await backups.create()

    async def recover(self, recovery_key: str) -> None:
        store = await SecretStorage(self.engine).open_secret_store(recovery_key)
        await store.import_secrets()
        await self.engine.backups().download()
        await self.refresh()

    async def recover_and_fix_backup(self, recovery_key: str) -> None:
        await self.recover(recovery_key)
        await self.enable_backup()

    async def reset_key(self, *, passphrase: str | None = None) -> str:
        if not all(self.engine.store.get(f"secret/{name}") for name in SECRETS):
            raise CryptoError("Recover all secrets before resetting the recovery key")
        store = await SecretStorage(self.engine).create_secret_store(
            passphrase=passphrase
        )
        await self.refresh()
        return store.secret_storage_key()

    async def recover_and_reset(
        self, old_key: str, *, passphrase: str | None = None
    ) -> str:
        await self.recover(old_key)
        return await self.reset_key(passphrase=passphrase)

    async def reset_identity(self) -> IdentityResetHandle | None:
        try:
            await self.engine.cross_signing.bootstrap(reset=True)
        except InteractiveAuthRequired as exc:
            handle = IdentityResetHandle(self.engine, exc)
            await self.engine.emit(
                "authentication", {"challenge": exc.body}, handle=handle
            )
            return handle
        return None

    async def disable(self) -> None:
        await self.engine.call(
            "set_account_data", event_type="m.secret_storage.default_key", content={}
        )
        await self.engine.backups().disable()
        await self.refresh()

    async def is_last_device(self) -> bool:
        await self.engine.devices.query(self.engine, [self.engine.user_id])
        return (
            len(self.engine.devices.get_device_keys_for_user(self.engine.user_id)) <= 1
        )
