"""Default-on Matrix E2EE using upstream python3-olm.

The engine owns cryptographic state and reliable outgoing requests. Network I/O
continues to use the adapter's transport and authentication implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from olm.group_session import OlmGroupSessionError

from .account import OlmAccountManager
from .attachments import decrypt_attachment, export_room_keys, import_room_keys
from .device_keys import DeviceKeyStore
from .identities import CrossSigning, Device, UserDevices, UserIdentity
from .megolm import MegolmManager
from .primitives import MEGOLM_ALGORITHM, OLM_ALGORITHM, canonical_json
from .recovery import Backups, Recovery
from .secret_storage import SECRETS, SecretStorage
from .sessions import OlmSessionManager
from .store import CryptoStore
from .types import (
    BackupDownloadStrategy,
    CollectStrategy,
    CryptoError,
    DecryptionError,
    LocalTrust,
    Observable,
    TaskLock,
    VerificationState,
)
from .verification import VerificationManager
from ..api.model import RawMatrixEvent
from ..api.types import UserId
from ..exception import ActionFailed
from ..utils import log


class CryptoEngine:
    def __init__(self, bot: Any, adapter: Any) -> None:
        self.bot, self.adapter = bot, adapter
        self.user_id, self.device_id = str(bot.user_id), bot.device_id
        if not self.device_id:
            raise CryptoError(
                "Default E2EE requires a device ID; use a device-bound token or explicitly disable E2EE"
            )
        identity = (bot.bot_info.homeserver.rstrip("/"), self.user_id, self.device_id)
        digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        if bot.bot_info.e2ee_store_path:
            directory = Path(bot.bot_info.e2ee_store_path)
        elif adapter.matrix_config.matrix_token_store_path:
            directory = (
                Path(adapter.matrix_config.matrix_token_store_path).parent
                / "e2ee"
                / digest
            )
        else:
            directory = Path(".data/matrix/e2ee") / digest
        self.store = CryptoStore(directory, identity)
        self.account = OlmAccountManager(self.store)
        self.sessions = OlmSessionManager(self.store, self.account)
        self.megolm = MegolmManager(self.store)
        self.devices = DeviceKeyStore(self.store)
        self.cross_signing = CrossSigning(self)
        self.verifications = VerificationManager(self)
        self._backups, self._recovery = Backups(self), Recovery(self)
        self._verification_state = Observable(VerificationState.UNKNOWN)
        self._lock = TaskLock()
        self._outgoing_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.ready = False
        for name in self.store.names("room/"):
            self.bot._encryption_states[name.removeprefix("room/")] = self.store.get(
                name
            )
        # Deprecated compatibility spellings for existing adapter extensions.
        self._account, self._sessions, self._megolm = (
            self.account,
            self.sessions,
            self.megolm,
        )
        self._store, self._device_keys = self.store, self.devices

    async def initialize(self) -> None:
        self.account.load_or_create()
        await self.devices.query(self, [self.user_id])
        remote = self.devices.get_device_key(self.user_id, self.device_id)
        expected = self.account.build_device_keys(self.bot)
        if remote and remote["keys"]["keys"] != expected["keys"]:
            raise CryptoError(
                "Device identity already exists with different keys; use a new device and store"
            )
        await self.flush_requests()
        if not self.store.get("identity_uploaded", False):
            await self.request(
                "keys_upload", {"device_keys": expected}, purpose="identity_upload"
            )
        await self.devices.query(self, [self.user_id])
        if self.devices.get_device_key(self.user_id, self.device_id):
            self.devices.set_trust(self.user_id, self.device_id, LocalTrust.VERIFIED)
        response = await self.call("keys_upload")
        await self.account.replenish(
            self, response.get("one_time_key_counts", {}), None
        )
        self.ready = True
        info = self.bot.bot_info
        if info.auto_enable_cross_signing:
            await self.cross_signing.bootstrap()
        credential = info.recovery_key or info.secret_storage_passphrase
        if credential:
            await self.recovery().recover(credential)
        elif info.auto_enable_backups:
            await self.recovery().enable_backup()
        self.update_verification_state()

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.store.close()

    async def call(self, api: str, **body: Any) -> Any:
        method = getattr(self.adapter, f"_api_{api}")
        return await method(self.bot, **body)

    async def emit(
        self, kind: str, content: dict[str, Any], *, handle: Any = None
    ) -> None:
        from ..event import CRYPTO_EVENT_CLASSES

        event = CRYPTO_EVENT_CLASSES[kind](
            type=f"matrix.crypto.{kind}",
            content=content,
            sender=content.get("user_id", self.user_id),
            room_id=content.get("room_id"),
            handle=handle,
        )
        # Plugin handlers can call back into the engine. Dispatch outside the
        # mutation lock, and track tasks so shutdown never leaves users waiting.
        task = asyncio.create_task(self.bot.handle_event(event))
        self._tasks.add(task)
        task.add_done_callback(self._event_done)

    def _event_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log("ERROR", f"E2EE plugin event handler failed: {task.exception()}")

    async def request(
        self,
        api: str,
        body: dict[str, Any],
        *,
        purpose: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        request_id = uuid4().hex
        record = {
            "api": api,
            "body": body,
            "purpose": purpose,
            "metadata": metadata or {},
        }
        self.store.put(f"outgoing/{request_id}", record)
        async with self._outgoing_lock:
            return await self._execute(request_id, record)

    async def _execute(self, request_id: str, record: dict[str, Any]) -> Any:
        result = await self.call(record["api"], **record["body"])
        if record["api"] == "keys_signatures_upload" and result.get("failures"):
            raise CryptoError("Server rejected signatures")
        with self.store.transaction():
            if record["purpose"] == "publish_keys":
                self.account.published(record["body"])
            elif record["purpose"] == "identity_upload":
                self.store.put("identity_uploaded", True)
            elif record["purpose"] == "backup_upload":
                for name in record["metadata"]["names"]:
                    value = self.store.get(name)
                    if value:
                        value["backed_up"] = record["metadata"]["version"]
                        self.store.put(name, value)
            self.store.delete(f"outgoing/{request_id}")
        return result

    async def flush_requests(self) -> None:
        async with self._outgoing_lock:
            for name in self.store.names("outgoing/"):
                await self._execute(
                    name.removeprefix("outgoing/"), self.store.get(name)
                )

    async def send_to_device(
        self, user_id: str, device_id: str, event_type: str, content: dict[str, Any]
    ) -> None:
        await self.request(
            "send_to_device",
            {
                "event_type": event_type,
                "txn_id": uuid4().hex,
                "messages": {user_id: {device_id: content}},
            },
        )

    async def send_encrypted_to_device(
        self, user_id: str, device_id: str, event_type: str, content: dict[str, Any]
    ) -> None:
        record = self.devices.get_device_key(user_id, device_id)
        if record is None:
            await self.devices.query(self, [user_id])
            record = self.devices.get_device_key(user_id, device_id)
        if not record or record.get("local_trust") == LocalTrust.BLACKLISTED:
            raise CryptoError("Unknown or blacklisted device")
        keys = record["keys"]["keys"]
        curve, ed = keys[f"curve25519:{device_id}"], keys[f"ed25519:{device_id}"]
        session = self.sessions.get(curve)
        if session is None:
            key = await self.devices.claim(self, user_id, device_id)
            session = self.sessions.create_outbound_session(curve, key)
        identity = self.account.account.identity_keys
        payload = {
            "sender": self.user_id,
            "sender_device": self.device_id,
            "keys": {"ed25519": identity["ed25519"]},
            "recipient": user_id,
            "recipient_keys": {"ed25519": ed},
            "type": event_type,
            "content": content,
        }
        request_id = uuid4().hex
        with self.store.transaction():
            encrypted = self.sessions.encrypt(session, curve, canonical_json(payload))
            body = {
                "algorithm": OLM_ALGORITHM,
                "sender_key": identity["curve25519"],
                "ciphertext": {curve: encrypted},
            }
            request = {
                "api": "send_to_device",
                "body": {
                    "event_type": "m.room.encrypted",
                    "txn_id": request_id,
                    "messages": {user_id: {device_id: body}},
                },
                "purpose": "",
                "metadata": {},
            }
            self.store.put(f"outgoing/{request_id}", request)
        async with self._outgoing_lock:
            await self._execute(request_id, request)

    async def handle_to_device_event(self, raw: RawMatrixEvent) -> None:
        async with self._lock:
            await self._receive_to_device(raw)

    async def _receive_to_device(self, raw: RawMatrixEvent) -> None:
        sender, content = str(raw.sender or ""), raw.content
        if raw.type == "m.room.encrypted":
            if content.get("algorithm") != OLM_ALGORITHM:
                raise DecryptionError("UnsupportedAlgorithm")
            curve = self.account.account.identity_keys["curve25519"]
            entry = content.get("ciphertext", {}).get(curve)
            if entry is None:
                return
            plaintext = self.sessions.decrypt_to_device_message(
                entry["body"], entry["type"], content["sender_key"]
            )
            inner = json.loads(plaintext)
            identity = self.account.account.identity_keys
            if (
                inner.get("sender") != sender
                or inner.get("recipient") != self.user_id
                or inner.get("recipient_keys", {}).get("ed25519") != identity["ed25519"]
            ):
                raise DecryptionError("OlmEnvelopeMismatch")
            device_id = inner.get("sender_device")
            if device_id is None:
                await self.devices.query(self, [sender])
                for candidate, record in self.devices.get_device_keys_for_user(
                    sender
                ).items():
                    if (
                        record["keys"]["keys"].get(f"curve25519:{candidate}")
                        == content["sender_key"]
                    ):
                        device_id = candidate
                        break
            device = self.devices.get_device_key(sender, device_id or "")
            if device is None:
                await self.devices.query(self, [sender])
                device = self.devices.get_device_key(sender, device_id or "")
            if (
                not isinstance(device_id, str)
                or not device
                or device["keys"]["keys"].get(f"curve25519:{device_id}")
                != content["sender_key"]
                or device["keys"]["keys"].get(f"ed25519:{device_id}")
                != inner.get("keys", {}).get("ed25519")
            ):
                raise DecryptionError("OlmSenderIdentityMismatch")
            await self._receive_authenticated(
                inner["type"],
                inner["content"],
                sender,
                device_id,
                content["sender_key"],
                inner["keys"]["ed25519"],
            )
        elif raw.type.startswith("m.key.verification."):
            await self.verifications.handle(raw)
        elif raw.type == "m.room_key_request":
            await self._key_request(sender, content)
        elif raw.type == "m.secret.request":
            await self._secret_request(sender, content)
        elif raw.type == "m.room_key.withheld":
            self.store.put(
                f"withheld/{content.get('room_id')}/{content.get('session_id')}",
                content,
            )
            await self.emit(
                "decryption",
                {
                    "room_id": content.get("room_id"),
                    "code": content.get("code"),
                    "user_id": sender,
                },
            )
        # Room keys and secrets are accepted only from authenticated Olm envelopes.

    async def _receive_authenticated(
        self,
        event_type: str,
        content: dict[str, Any],
        sender: str,
        device_id: str,
        sender_key: str,
        ed25519: str,
    ) -> None:
        if event_type in {"m.room_key", "m.forwarded_room_key"}:
            value = dict(content)
            forwarded = event_type == "m.forwarded_room_key"
            if forwarded:
                request = self.store.get(
                    f"key_request/{content.get('room_id')}/{content.get('session_id')}"
                )
                if (
                    sender != self.user_id
                    or not self.devices.is_verified(sender, device_id)
                    or not request
                ):
                    return
                if content.get("sender_key") != request["sender_key"]:
                    raise DecryptionError("ForwardedKeyMismatch")
                value["sender_claimed_keys"] = {
                    "ed25519": value.pop("sender_claimed_ed25519_key")
                }
                value["forwarding_curve25519_key_chain"] = [
                    *value.get("forwarding_curve25519_key_chain", []),
                    sender_key,
                ]
            else:
                value.update(
                    sender_key=sender_key,
                    sender=sender,
                    sender_claimed_keys={"ed25519": ed25519},
                    forwarding_curve25519_key_chain=[],
                )
            self.megolm.import_key(value, exported=forwarded)
            await self.retry_decryption()
        elif event_type == "m.secret.send":
            request = self.store.get(f"secret_request/{content.get('request_id')}")
            if (
                not request
                or sender != self.user_id
                or device_id not in request["devices"]
                or not self.devices.is_verified(sender, device_id)
            ):
                return
            await self.import_secret(request["name"], content["secret"])
            self.store.delete(f"secret_request/{content['request_id']}")
        elif event_type.startswith("m.key.verification."):
            await self.verifications.handle(
                RawMatrixEvent(type=event_type, sender=UserId(sender), content=content)
            )

    async def _key_request(self, sender: str, content: dict[str, Any]) -> None:
        if content.get("action") != "request":
            return
        device_id = content.get("requesting_device_id")
        body = content.get("body", {})
        if not isinstance(device_id, str) or body.get("algorithm") != MEGOLM_ALGORITHM:
            return
        # Historical keys are forwarded only to verified devices of our own
        # user. Other users receive current keys through normal room sharing.
        if sender != self.user_id or not self.devices.is_verified(sender, device_id):
            return
        values = self.megolm.export_keys(body.get("room_id"), body.get("session_id"))
        if not values or values[0]["sender_key"] != body.get("sender_key"):
            return
        value = values[0]
        value["sender_claimed_ed25519_key"] = value.pop("sender_claimed_keys")[
            "ed25519"
        ]
        await self.send_encrypted_to_device(
            sender, device_id, "m.forwarded_room_key", value
        )

    async def request_room_key(self, room_id: str, content: dict[str, Any]) -> None:
        session_id = content.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        sender_key = content.get("sender_key")
        if not isinstance(sender_key, str) or not sender_key:
            return
        name = f"key_request/{room_id}/{session_id}"
        if self.store.get(name):
            return
        request_id = uuid4().hex
        body = {
            "room_id": room_id,
            "algorithm": MEGOLM_ALGORITHM,
            "session_id": session_id,
            "sender_key": sender_key,
        }
        self.store.put(name, {**body, "request_id": request_id})
        await self.send_to_device(
            self.user_id,
            "*",
            "m.room_key_request",
            {
                "action": "request",
                "request_id": request_id,
                "requesting_device_id": self.device_id,
                "body": body,
            },
        )

    async def request_secrets(self, device_id: str) -> None:
        for name in SECRETS:
            if self.store.get(f"secret/{name}"):
                continue
            request_id = uuid4().hex
            self.store.put(
                f"secret_request/{request_id}", {"name": name, "devices": [device_id]}
            )
            await self.send_to_device(
                self.user_id,
                device_id,
                "m.secret.request",
                {
                    "action": "request",
                    "name": name,
                    "request_id": request_id,
                    "requesting_device_id": self.device_id,
                },
            )

    async def _secret_request(self, sender: str, content: dict[str, Any]) -> None:
        device_id = content.get("requesting_device_id")
        if (
            content.get("action") != "request"
            or sender != self.user_id
            or not device_id
            or not self.devices.is_verified(sender, device_id)
            or content.get("name") not in SECRETS
        ):
            return
        secret = self.store.get(f"secret/{content['name']}")
        if secret:
            await self.send_encrypted_to_device(
                sender,
                device_id,
                "m.secret.send",
                {"request_id": content["request_id"], "secret": secret},
            )

    async def import_secret(self, name: str, secret: str) -> None:
        if name.startswith("m.cross_signing."):
            await self.cross_signing.import_secret(name, secret)
        elif name == "m.megolm_backup.v1":
            await self.backups().enable(secret)
            if (
                self.bot.bot_info.backup_download_strategy
                == BackupDownloadStrategy.ONE_SHOT
            ):
                await self.backups().download()
        else:
            raise ValueError("Unknown secret")
        self.update_verification_state()

    async def receive_sync(self, sync: Any) -> None:
        async with self._lock:
            await self.flush_requests()
            if sync.device_lists:
                changed = [str(uid) for uid in sync.device_lists.changed]
                await self.devices.query(self, changed)
                if changed or sync.device_lists.left:
                    self.invalidate_outbound_sessions()
                await self.cross_signing.refresh_trust()
            if sync.to_device:
                for raw in sync.to_device.events:
                    try:
                        await self._receive_to_device(raw)
                    except (
                        DecryptionError,
                        CryptoError,
                        ValueError,
                        KeyError,
                        OlmGroupSessionError,
                    ) as exc:
                        log(
                            "WARNING",
                            f"Rejected Matrix to-device event: {type(exc).__name__}: {exc}",
                        )
            await self.account.replenish(
                self,
                sync.device_one_time_keys_count,
                sync.device_unused_fallback_key_types,
            )
            await self.verifications.expire()
            self.update_verification_state()
        if await self.backups().are_enabled():
            await self.backups().upload()

    def update_verification_state(self) -> None:
        identity = self.devices.identity(self.user_id)
        verified = bool(
            identity
            and identity.get("verified")
            and self.devices.is_cross_signed(self.user_id, self.device_id)
        )
        self._verification_state.set(
            VerificationState.VERIFIED if verified else VerificationState.UNVERIFIED
        )

    def verification_state(self) -> Any:
        return self._verification_state.changes()

    def invalidate_outbound_sessions(self, room_id: str | None = None) -> None:
        for name in self.store.names("outbound/"):
            if room_id is None or name == f"outbound/{room_id}":
                self.store.delete(name)

    def mark_room_as_encrypted(
        self, room_id: str, algorithm: str, **settings: Any
    ) -> None:
        self.store.put(f"room/{room_id}", {"algorithm": algorithm, **settings})

    def is_room_encrypted(self, room_id: str) -> bool:
        return bool(self.store.get(f"room/{room_id}"))

    async def room_settings(self, room_id: str) -> dict[str, Any] | None:
        settings = await self.bot._room_encryption_settings(room_id)
        if settings:
            if settings.get("algorithm") != MEGOLM_ALGORITHM:
                raise CryptoError("Unsupported room encryption algorithm")
            self.store.put(f"room/{room_id}", settings)
        return settings

    async def _share_room(
        self, room_id: str, settings: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        members = await self.call("get_room_members", room_id=room_id)
        history = settings.get("history_visibility", "shared")
        allowed = (
            {"join", "invite"} if history in {"shared", "world_readable"} else {"join"}
        )
        users = sorted(
            {
                str(event.state_key)
                for event in members.chunk
                if event.state_key and event.content.get("membership") in allowed
            }
        )
        await self.devices.query(self, users)
        identity = {**self.account.account.identity_keys, "user_id": self.user_id}
        session, record = self.megolm.outbound(room_id, settings, identity)
        recipient_ids = [
            f"{uid}|{did}"
            for uid in users
            for did in self.devices.get_device_keys_for_user(uid)
        ]
        if (
            set(record["recipients"]) - set(recipient_ids)
            or record["history_visibility"] != history
        ):
            self.megolm.discard(room_id)
            session, record = self.megolm.outbound(room_id, settings, identity)
        record["recipients"] = recipient_ids
        strategy = CollectStrategy(self.bot.bot_info.encryption_sharing_strategy)
        for uid in users:
            for did, device in self.devices.get_device_keys_for_user(uid).items():
                if (uid, did) == (self.user_id, self.device_id):
                    continue
                keys = device["keys"].get("keys", {})
                curve = keys.get(f"curve25519:{did}")
                if not curve:
                    continue
                recipient = f"{uid}|{did}|{curve}"
                if recipient in record["shared"]:
                    continue
                trusted = self.devices.is_verified(uid, did)
                cross_signed = self.devices.is_cross_signed(uid, did)
                identity_record = self.devices.identity(uid)
                blocked = device.get("local_trust") == LocalTrust.BLACKLISTED
                if (
                    strategy == CollectStrategy.ERROR_ON_VERIFIED_USER_PROBLEM
                    and identity_record
                    and (
                        identity_record.get("violation")
                        or (identity_record.get("verified") and not cross_signed)
                    )
                    and not blocked
                    and device.get("local_trust")
                    not in {LocalTrust.IGNORED, LocalTrust.VERIFIED}
                ):
                    raise CryptoError(
                        "Verified user identity or device requires review"
                    )
                excluded = (
                    blocked
                    or (
                        strategy == CollectStrategy.ONLY_TRUSTED_DEVICES and not trusted
                    )
                    or (strategy == CollectStrategy.IDENTITY_BASED and not cross_signed)
                )
                if excluded:
                    await self.send_to_device(
                        uid,
                        did,
                        "m.room_key.withheld",
                        {
                            "algorithm": MEGOLM_ALGORITHM,
                            "room_id": room_id,
                            "session_id": session.id,
                            "sender_key": identity["curve25519"],
                            "code": "m.blacklisted" if blocked else "m.unverified",
                            "reason": "Device excluded by sharing policy",
                        },
                    )
                    continue
                await self.send_encrypted_to_device(
                    uid,
                    did,
                    "m.room_key",
                    {
                        "algorithm": MEGOLM_ALGORITHM,
                        "room_id": room_id,
                        "session_id": session.id,
                        "session_key": session.session_key,
                    },
                )
                record["shared"][recipient] = session.message_index
                self.megolm.save_outbound(room_id, session, record)
        self.megolm.save_outbound(room_id, session, record)
        return session, record

    async def send_room_event(
        self, room_id: str, event_type: str, content: dict[str, Any], txn_id: str
    ) -> Any:
        async with self._lock:
            settings = await self.room_settings(room_id)
            if settings and event_type != "m.room.encrypted":
                if not self.ready:
                    raise CryptoError("E2EE initialization has not completed")
                await self.flush_requests()
                session, record = await self._share_room(room_id, settings)
                with self.store.transaction():
                    ciphertext = session.encrypt(
                        canonical_json(
                            {"room_id": room_id, "type": event_type, "content": content}
                        )
                    )
                    self.megolm.save_outbound(room_id, session, record)
                    content = {
                        "algorithm": MEGOLM_ALGORITHM,
                        "session_id": session.id,
                        "sender_key": self.account.account.identity_keys["curve25519"],
                        "device_id": self.device_id,
                        "ciphertext": ciphertext,
                    }
                    event_type = "m.room.encrypted"
                    request_id = uuid4().hex
                    request = {
                        "api": "send_event",
                        "body": {
                            "room_id": room_id,
                            "event_type": event_type,
                            "content": content,
                            "txn_id": txn_id,
                        },
                        "purpose": "",
                        "metadata": {},
                    }
                    self.store.put(f"outgoing/{request_id}", request)
                async with self._outgoing_lock:
                    return await self._execute(request_id, request)
            return await self.call(
                "send_event",
                room_id=room_id,
                event_type=event_type,
                content=content,
                txn_id=txn_id,
            )

    async def decrypt_room_event(
        self, raw: RawMatrixEvent, *, room_id: str
    ) -> RawMatrixEvent | None:
        try:
            value = self.megolm.decrypt(room_id, raw.model_dump())
            return RawMatrixEvent.model_validate(value)
        except DecryptionError as exc:
            if exc.code == "MissingRoomKey":
                event_id = str(raw.event_id or uuid4().hex)
                self.store.put(
                    f"undecrypted/{event_id}",
                    {"room_id": room_id, "event": raw.model_dump(mode="json")},
                )
                await self.request_room_key(room_id, raw.content)
                if (
                    self.bot.bot_info.backup_download_strategy
                    == BackupDownloadStrategy.AFTER_DECRYPTION_FAILURE
                    and await self.backups().are_enabled()
                ):
                    await self.backups().download_room_key(
                        room_id, raw.content["session_id"]
                    )
                    try:
                        return RawMatrixEvent.model_validate(
                            self.megolm.decrypt(room_id, raw.model_dump())
                        )
                    except DecryptionError:
                        pass
            await self.emit(
                "decryption",
                {"room_id": room_id, "event_id": raw.event_id, "code": exc.code},
            )
            return None

    async def retry_decryption(self) -> None:
        for name in self.store.names("undecrypted/"):
            pending = self.store.get(name)
            try:
                self.megolm.decrypt(pending["room_id"], pending["event"])
            except DecryptionError:
                continue
            raw = RawMatrixEvent.model_validate(pending["event"])
            self.store.delete(name)
            task = asyncio.create_task(
                self.adapter._dispatch_room_event(
                    self.bot, raw, room_id=pending["room_id"]
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._event_done)

    async def handle_verification_event(
        self, raw: RawMatrixEvent, room_id: str | None = None
    ) -> bool:
        async with self._lock:
            return await self.verifications.handle(raw, room_id)

    def is_sender_trusted(self, record: dict[str, Any]) -> bool:
        for did, device in self.devices.get_device_keys_for_user(
            record.get("sender", "")
        ).items():
            if device["keys"]["keys"].get(f"curve25519:{did}") == record["sender_key"]:
                return self.devices.is_verified(record["sender"], did)
        return False

    async def account_data(self, event_type: str) -> dict[str, Any] | None:
        try:
            return await self.call("get_account_data", event_type=event_type)
        except ActionFailed as exc:
            if exc.status_code == 404:
                return None
            raise

    def secret_storage(self) -> SecretStorage:
        return SecretStorage(self)

    def backups(self) -> Backups:
        return self._backups

    def recovery(self) -> Recovery:
        return self._recovery

    async def get_device(self, user_id: str, device_id: str) -> Device | None:
        await self.devices.query(self, [user_id])
        return (
            Device(self, user_id, device_id)
            if self.devices.get_device_key(user_id, device_id)
            else None
        )

    async def get_own_device(self) -> Device | None:
        return await self.get_device(self.user_id, self.device_id)

    async def get_user_devices(self, user_id: str) -> UserDevices:
        await self.devices.query(self, [user_id])
        return UserDevices(self, user_id)

    async def get_user_identity(self, user_id: str) -> UserIdentity | None:
        await self.devices.query(self, [user_id])
        return UserIdentity(self, user_id) if self.devices.identity(user_id) else None

    async def get_verification_request(self, user_id: str, flow_id: str) -> Any:
        return self.verifications.requests.get((user_id, flow_id))

    async def get_verification(self, user_id: str, flow_id: str) -> Any:
        request = await self.get_verification_request(user_id, flow_id)
        return request.verification if request else None

    async def bootstrap_cross_signing(
        self, auth_data: dict[str, Any] | None = None
    ) -> None:
        await self.cross_signing.bootstrap(auth_data)

    async def cross_signing_status(self) -> dict[str, bool]:
        return self.cross_signing.status()

    async def export_room_keys(self, passphrase: str) -> str:
        return export_room_keys(self.megolm.export_keys(), passphrase)

    async def import_room_keys(self, exported: str, passphrase: str) -> int:
        count = 0
        with self.store.transaction():
            for value in import_room_keys(exported, passphrase):
                count += self.megolm.import_key(value)
        await self.retry_decryption()
        return count

    async def download_encrypted_file(self, descriptor: dict[str, Any]) -> bytes:
        uri = descriptor["url"]
        if not uri.startswith("mxc://") or "/" not in uri[6:]:
            raise ValueError("Invalid encrypted attachment URI")
        server, media_id = uri[6:].split("/", 1)
        response = await self.call(
            "download_media", server_name=server, media_id=media_id
        )
        if not isinstance(response, bytes):
            raise CryptoError("Encrypted media download returned non-binary content")
        return decrypt_attachment(response, descriptor)


Encryption = CryptoEngine
__all__ = ("CryptoEngine", "Encryption")
