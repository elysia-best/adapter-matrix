"""Interactive Matrix SAS and QR verification, backed by upstream libolm."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import os
from time import monotonic, time
from typing import Any
from uuid import uuid4

from olm.sas import OlmSasError, Sas

from .device_keys import signing_key
from .identities import Device
from .primitives import b64d, b64e, canonical_json
from .types import CryptoError, LocalTrust, Observable

METHODS = ["m.sas.v1", "m.qr_code.show.v1", "m.qr_code.scan.v1", "m.reciprocate.v1"]
MACS = ["hkdf-hmac-sha256.v2", "hkdf-hmac-sha256"]


class VerificationRequestState(str, Enum):
    CREATED = "Created"
    REQUESTED = "Requested"
    READY = "Ready"
    TRANSITIONED = "Transitioned"
    DONE = "Done"
    CANCELLED = "Cancelled"


class SasState(str, Enum):
    CREATED = "Created"
    STARTED = "Started"
    ACCEPTED = "Accepted"
    KEYS_EXCHANGED = "KeysExchanged"
    CONFIRMED = "Confirmed"
    DONE = "Done"
    CANCELLED = "Cancelled"


class QrVerificationState(str, Enum):
    STARTED = "Started"
    SCANNED = "Scanned"
    CONFIRMED = "Confirmed"
    RECIPROCATED = "Reciprocated"
    DONE = "Done"
    CANCELLED = "Cancelled"


@dataclass(frozen=True)
class Emoji:
    symbol: str
    description: str


_EMOJI_SYMBOLS = ["🐶", "🐱", "🦁", "🐎", "🦄", "🐷", "🐘", "🐰", "🐼", "🐓", "🐧", "🐢", "🐟", "🐙", "🦋", "🌷", "🌳", "🌵", "🍄", "🌏", "🌙", "☁️", "🔥", "🍌", "🍎", "🍓", "🌽", "🍕", "🎂", "❤️", "😀", "🤖", "🎩", "👓", "🔧", "🎅", "👍", "☂️", "⌛", "⏰", "🎁", "💡", "📕", "✏️", "📎", "✂️", "🔒", "🔑", "🔨", "☎️", "🏁", "🚂", "🚲", "✈️", "🚀", "🏆", "⚽", "🎸", "🎺", "🔔", "⚓", "🎧", "📁", "📌"]
_EMOJI_NAMES = ["Dog", "Cat", "Lion", "Horse", "Unicorn", "Pig", "Elephant", "Rabbit", "Panda", "Rooster", "Penguin", "Turtle", "Fish", "Octopus", "Butterfly", "Flower", "Tree", "Cactus", "Mushroom", "Globe", "Moon", "Cloud", "Fire", "Banana", "Apple", "Strawberry", "Corn", "Pizza", "Cake", "Heart", "Smiley", "Robot", "Hat", "Glasses", "Spanner", "Santa", "Thumbs Up", "Umbrella", "Hourglass", "Clock", "Gift", "Light Bulb", "Book", "Pencil", "Paperclip", "Scissors", "Lock", "Key", "Hammer", "Telephone", "Flag", "Train", "Bicycle", "Aeroplane", "Rocket", "Trophy", "Ball", "Guitar", "Trumpet", "Bell", "Anchor", "Headphones", "Folder", "Pin"]


@dataclass(frozen=True)
class QrVerificationData:
    mode: int
    flow_id: str
    first_key: str
    second_key: str
    secret: bytes

    def to_bytes(self) -> bytes:
        flow = self.flow_id.encode()
        first, second = b64d(self.first_key), b64d(self.second_key)
        if (
            self.mode not in (0, 1, 2)
            or len(first) != 32
            or len(second) != 32
            or len(self.secret) < 8
            or len(flow) > 65535
        ):
            raise ValueError("Invalid Matrix QR verification data")
        return (
            b"MATRIX\x02"
            + bytes([self.mode])
            + len(flow).to_bytes(2, "big")
            + flow
            + first
            + second
            + self.secret
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> QrVerificationData:
        if len(data) < 82 or data[:7] != b"MATRIX\x02" or data[7] not in (0, 1, 2):
            raise ValueError("Invalid Matrix QR verification header")
        length = int.from_bytes(data[8:10], "big")
        offset = 10 + length
        if len(data) < offset + 72:
            raise ValueError("Truncated Matrix QR verification data")
        return cls(
            data[7],
            data[10:offset].decode(),
            b64e(data[offset : offset + 32]),
            b64e(data[offset + 32 : offset + 64]),
            data[offset + 64 :],
        )


class VerificationRequest:
    def __init__(
        self,
        manager: VerificationManager,
        user_id: str,
        device_id: str,
        flow_id: str,
        *,
        room_id: str | None = None,
        we_started: bool = False,
        methods: list[str] | None = None,
    ) -> None:
        self.manager, self.engine = manager, manager.engine
        self.other_user_id, self.other_device_id = user_id, device_id
        self.flow_id, self.room_id, self.we_started = flow_id, room_id, we_started
        self.our_methods = list(METHODS)
        self.their_methods = methods or []
        self.created = monotonic()
        self._state = Observable(
            VerificationRequestState.CREATED
            if we_started
            else VerificationRequestState.REQUESTED
        )
        self.verification: SasVerification | QrVerification | None = None
        self.cancel_info: dict[str, Any] | None = None
        self.is_passive = False
        self._seen: set[str] = set()
        self._snapshot: dict[str, Any] = {}
        self._device_snapshot: dict[str, str] = {}

    def state(self) -> VerificationRequestState:
        return self._state.value

    def changes(self) -> Any:
        return self._state.changes()

    def is_ready(self) -> bool:
        return self.state() == VerificationRequestState.READY

    def is_done(self) -> bool:
        return self.state() == VerificationRequestState.DONE

    def is_cancelled(self) -> bool:
        return self.state() == VerificationRequestState.CANCELLED

    def is_self_verification(self) -> bool:
        return self.other_user_id == self.engine.user_id

    def their_supported_methods(self) -> list[str]:
        return list(self.their_methods)

    async def _change(self, state: VerificationRequestState) -> None:
        self._state.set(state)
        await self.engine.emit(
            "verification",
            {
                "flow_id": self.flow_id,
                "user_id": self.other_user_id,
                "state": state.value,
                "room_id": self.room_id,
            },
            handle=self,
        )

    def snapshot(self) -> None:
        device = self.engine.devices.get_device_key(
            self.other_user_id, self.other_device_id
        )
        if not device:
            raise CryptoError("Unknown verification device")
        self._device_snapshot = dict(device["keys"]["keys"])
        for uid in {self.engine.user_id, self.other_user_id}:
            identity = self.engine.devices.identity(uid)
            self._snapshot[uid] = identity["master"]["keys"] if identity else None

    def check_snapshot(self) -> None:
        device = self.engine.devices.get_device_key(
            self.other_user_id, self.other_device_id
        )
        if not device or device["keys"]["keys"] != self._device_snapshot:
            raise CryptoError("Device identity changed during verification")
        for uid, keys in self._snapshot.items():
            identity = self.engine.devices.identity(uid)
            if (identity["master"]["keys"] if identity else None) != keys:
                raise CryptoError("Cross-signing identity changed during verification")

    async def accept(self) -> None:
        await self.accept_with_methods(METHODS)

    async def accept_with_methods(self, methods: list[str]) -> None:
        if self.state() != VerificationRequestState.REQUESTED:
            return
        self.our_methods = [method for method in methods if method in METHODS]
        if not set(self.our_methods) & set(self.their_methods):
            await self.cancel("m.unknown_method")
            return
        self.snapshot()
        await self.send(
            "ready", {"from_device": self.engine.device_id, "methods": self.our_methods}
        )
        await self._change(VerificationRequestState.READY)

    async def start_sas(self) -> SasVerification | None:
        if not self.is_ready() or "m.sas.v1" not in self.their_methods:
            return None
        self.snapshot()
        sas = SasVerification(self, we_started=True)
        self.verification = sas
        sas.start_content = {
            "from_device": self.engine.device_id,
            "method": "m.sas.v1",
            "key_agreement_protocols": ["curve25519-hkdf-sha256"],
            "hashes": ["sha256"],
            "message_authentication_codes": MACS,
            "short_authentication_string": ["decimal", "emoji"],
        }
        sas.start_content = self.wire(sas.start_content)
        await self.send("start", sas.start_content)
        await self._change(VerificationRequestState.TRANSITIONED)
        return sas

    async def generate_qr_code(self) -> QrVerification | None:
        if (
            not self.is_ready()
            or "m.qr_code.scan.v1" not in self.their_methods
            or "m.qr_code.show.v1" not in self.our_methods
        ):
            return None
        self.snapshot()
        own_identity = self.engine.devices.identity(self.engine.user_id)
        other_identity = self.engine.devices.identity(self.other_user_id)
        if not own_identity or not other_identity:
            return None
        own_master = signing_key(own_identity["master"], self.engine.user_id, "master")
        if not self.is_self_verification():
            mode, first, second = (
                0,
                own_master,
                signing_key(other_identity["master"], self.other_user_id, "master"),
            )
        elif own_identity.get("verified"):
            mode, first, second = (
                1,
                own_master,
                self._device_snapshot[f"ed25519:{self.other_device_id}"],
            )
        else:
            mode, first, second = (
                2,
                self.engine.account.account.identity_keys["ed25519"],
                own_master,
            )
        qr = QrVerification(
            self,
            QrVerificationData(mode, self.flow_id, first, second, os.urandom(16)),
            scanned=False,
        )
        self.verification = qr
        await self._change(VerificationRequestState.TRANSITIONED)
        return qr

    async def scan_qr_code(
        self, data: bytes | QrVerificationData
    ) -> QrVerification | None:
        if (
            not self.is_ready()
            or "m.qr_code.show.v1" not in self.their_methods
            or "m.qr_code.scan.v1" not in self.our_methods
        ):
            return None
        self.snapshot()
        value = QrVerificationData.from_bytes(data) if isinstance(data, bytes) else data
        value.to_bytes()
        if value.flow_id != self.flow_id:
            raise CryptoError("QR verification flow mismatch")
        own = self.engine.devices.identity(self.engine.user_id)
        other = self.engine.devices.identity(self.other_user_id)
        if not own or not other:
            raise CryptoError("QR verification requires both cross-signing identities")
        own_master = signing_key(own["master"], self.engine.user_id, "master")
        other_master = signing_key(other["master"], self.other_user_id, "master")
        expected = {
            0: (other_master, own_master),
            1: (own_master, self.engine.account.account.identity_keys["ed25519"]),
            2: (self._device_snapshot[f"ed25519:{self.other_device_id}"], own_master),
        }[value.mode]
        if (value.mode == 0) == self.is_self_verification() or (
            value.first_key,
            value.second_key,
        ) != expected:
            raise CryptoError("QR verification identity mismatch")
        if value.mode == 2 and not own.get("verified"):
            raise CryptoError("Untrusted device cannot verify another untrusted device")
        qr = QrVerification(self, value, scanned=True)
        self.verification = qr
        await self.send(
            "start",
            {
                "from_device": self.engine.device_id,
                "method": "m.reciprocate.v1",
                "secret": b64e(value.secret),
            },
        )
        await self._change(VerificationRequestState.TRANSITIONED)
        return qr

    def wire(self, content: dict[str, Any]) -> dict[str, Any]:
        value = dict(content)
        if self.room_id:
            value["m.relates_to"] = {
                "rel_type": "m.reference",
                "event_id": self.flow_id,
            }
        else:
            value["transaction_id"] = self.flow_id
        return value

    async def send(self, kind: str, content: dict[str, Any]) -> None:
        event_type = f"m.key.verification.{kind}"
        if self.room_id:
            await self.engine.send_room_event(
                self.room_id, event_type, self.wire(content), uuid4().hex
            )
        else:
            await self.engine.send_to_device(
                self.other_user_id, self.other_device_id, event_type, self.wire(content)
            )

    async def cancel(self, code: str = "m.user", *, received: bool = False) -> None:
        if self.is_done() or self.is_cancelled():
            return
        self.cancel_info = {
            "code": code,
            "reason": code,
            "cancelled_by_us": not received,
        }
        if not received:
            await self.send("cancel", {"code": code, "reason": code})
        if isinstance(self.verification, SasVerification):
            self.verification._state.set(SasState.CANCELLED)
        elif isinstance(self.verification, QrVerification):
            self.verification._state.set(QrVerificationState.CANCELLED)
        await self._change(VerificationRequestState.CANCELLED)

    async def complete(self, *, trust_identity: bool) -> None:
        self.check_snapshot()
        self.engine.devices.set_trust(
            self.other_user_id, self.other_device_id, LocalTrust.VERIFIED
        )
        if trust_identity:
            await self.engine.cross_signing.trust_identity(self.other_user_id)
        if self.is_self_verification():
            if self.engine.cross_signing.status()["has_self_signing"]:
                await self.engine.cross_signing.sign_device(
                    Device(self.engine, self.other_user_id, self.other_device_id)
                )
        elif trust_identity and self.engine.cross_signing.status()["has_user_signing"]:
            await self.engine.cross_signing.sign_user(self.other_user_id)
        await self._change(VerificationRequestState.DONE)
        if self.is_self_verification():
            await self.engine.request_secrets(self.other_device_id)


class SasVerification:
    def __init__(self, request: VerificationRequest, *, we_started: bool) -> None:
        self.request = request
        self.we_started = we_started
        self.sas = Sas()
        self.start_content: dict[str, Any] = {}
        self.accept_content: dict[str, Any] = {}
        self.their_key: str | None = None
        self.mac_method = MACS[0]
        self.confirmed = False
        self.mac_received = False
        self.done_received = False
        self.done_sent = False
        self.trust_identity = False
        self.created = self.last_event = monotonic()
        self._state = Observable(SasState.CREATED if we_started else SasState.STARTED)

    def state(self) -> SasState:
        return self._state.value

    def changes(self) -> Any:
        return self._state.changes()

    def is_done(self) -> bool:
        return self.request.is_done()

    def is_cancelled(self) -> bool:
        return self.request.is_cancelled()

    def can_be_presented(self) -> bool:
        return (
            self.their_key is not None
            and not self.is_cancelled()
            and not self.is_done()
        )

    async def _change(self, state: SasState) -> None:
        self._state.set(state)
        await self.request.engine.emit(
            "verification",
            {
                "flow_id": self.request.flow_id,
                "user_id": self.request.other_user_id,
                "state": state.value,
                "method": "m.sas.v1",
            },
            handle=self,
        )

    def _bytes(self) -> bytes | None:
        if self.their_key is None:
            return None
        request, engine = self.request, self.request.engine
        ours = f"{engine.user_id}|{engine.device_id}|{self.sas.pubkey}"
        theirs = f"{request.other_user_id}|{request.other_device_id}|{self.their_key}"
        first, second = (ours, theirs) if self.we_started else (theirs, ours)
        return self.sas.generate_bytes(
            f"MATRIX_KEY_VERIFICATION_SAS|{first}|{second}|{request.flow_id}", 6
        )

    def decimals(self) -> tuple[int, int, int] | None:
        value = self._bytes()
        if value is None:
            return None
        return (
            ((value[0] << 5 | value[1] >> 3) % 9000) + 1000,
            (((value[1] & 7) << 10 | value[2] << 2 | value[3] >> 6) % 9000) + 1000,
            ((((value[3] & 63) << 7 | value[4] >> 1) % 9000) + 1000),
        )

    def emoji(self) -> tuple[Emoji, ...] | None:
        value = self._bytes()
        if value is None or not self.supports_emoji():
            return None
        bits = int.from_bytes(value, "big")
        indices = [(bits >> shift) & 63 for shift in range(42, 5, -6)]
        return tuple(Emoji(_EMOJI_SYMBOLS[i], _EMOJI_NAMES[i]) for i in indices)

    def supports_emoji(self) -> bool:
        return "emoji" in self.accept_content.get("short_authentication_string", [])

    async def accept(self) -> None:
        if self.we_started or self.state() != SasState.STARTED:
            return
        start = self.start_content
        if "curve25519-hkdf-sha256" not in start.get(
            "key_agreement_protocols", []
        ) or "sha256" not in start.get("hashes", []):
            await self.request.cancel("m.unknown_method")
            return
        common_mac = [
            method
            for method in MACS
            if method in start.get("message_authentication_codes", [])
        ]
        common_sas = [
            method
            for method in ("decimal", "emoji")
            if method in start.get("short_authentication_string", [])
        ]
        if not common_mac or not common_sas:
            await self.request.cancel("m.unknown_method")
            return
        self.mac_method = common_mac[0]
        self.accept_content = {
            "method": "m.sas.v1",
            "key_agreement_protocol": "curve25519-hkdf-sha256",
            "hash": "sha256",
            "message_authentication_code": self.mac_method,
            "short_authentication_string": common_sas,
            "commitment": b64e(
                hashlib.sha256(
                    (self.sas.pubkey + canonical_json(start)).encode()
                ).digest()
            ),
        }
        await self.request.send("accept", self.accept_content)
        await self._change(SasState.ACCEPTED)

    def _mac(self, value: str, info: str) -> str:
        calculate = (
            self.sas.calculate_mac_fixed_base64
            if self.mac_method == MACS[0]
            else self.sas.calculate_mac
        )
        return calculate(value, info)

    def _info(self, *, sending: bool) -> str:
        request, engine = self.request, self.request.engine
        ours, theirs = (
            engine.user_id + engine.device_id,
            request.other_user_id + request.other_device_id,
        )
        first, second = (ours, theirs) if sending else (theirs, ours)
        return f"MATRIX_KEY_VERIFICATION_MAC{first}{second}{request.flow_id}"

    async def confirm(self) -> None:
        if not self.can_be_presented() or self.confirmed:
            return
        request, engine = self.request, self.request.engine
        request.check_snapshot()
        keys = {
            f"ed25519:{engine.device_id}": engine.account.account.identity_keys[
                "ed25519"
            ]
        }
        identity = engine.devices.identity(engine.user_id)
        if identity and identity.get("verified"):
            public = signing_key(identity["master"], engine.user_id, "master")
            keys[f"ed25519:{public}"] = public
        info = self._info(sending=True)
        await request.send(
            "mac",
            {
                "mac": {
                    key: self._mac(value, info + key) for key, value in keys.items()
                },
                "keys": self._mac(",".join(sorted(keys)), info + "KEY_IDS"),
            },
        )
        self.confirmed = True
        await self._change(SasState.CONFIRMED)
        await self._finish()

    async def mismatch(self) -> None:
        await self.request.cancel("m.mismatched_sas")

    async def cancel(self) -> None:
        await self.request.cancel()

    async def _finish(self) -> None:
        if self.confirmed and self.mac_received:
            if not self.done_sent:
                await self.request.send("done", {})
                self.done_sent = True
            if self.done_received:
                await self.request.complete(trust_identity=self.trust_identity)
                await self._change(SasState.DONE)

    async def handle(self, kind: str, content: dict[str, Any]) -> None:
        self.last_event = monotonic()
        if kind == "accept":
            if not self.we_started or self.state() != SasState.CREATED:
                raise CryptoError("Unexpected SAS accept")
            if (
                content.get("key_agreement_protocol") != "curve25519-hkdf-sha256"
                or content.get("hash") != "sha256"
                or content.get("message_authentication_code") not in MACS
            ):
                raise CryptoError("Unsupported SAS parameters")
            if not content.get("short_authentication_string") or not set(
                content["short_authentication_string"]
            ) <= {"emoji", "decimal"}:
                raise CryptoError("Invalid SAS display methods")
            self.accept_content = content
            self.mac_method = content["message_authentication_code"]
            await self.request.send("key", {"key": self.sas.pubkey})
            await self._change(SasState.ACCEPTED)
        elif kind == "key":
            key = content["key"]
            if (
                self.state() != SasState.ACCEPTED
                or not isinstance(key, str)
                or len(b64d(key)) != 32
            ):
                raise CryptoError("Unexpected SAS key")
            if self.we_started:
                commitment = b64e(
                    hashlib.sha256(
                        (key + canonical_json(self.start_content)).encode()
                    ).digest()
                )
                if not hmac.compare_digest(
                    commitment, self.accept_content.get("commitment", "")
                ):
                    await self.request.cancel("m.mismatched_commitment")
                    return
            self.their_key = key
            self.sas.set_their_pubkey(key)
            if not self.we_started:
                await self.request.send("key", {"key": self.sas.pubkey})
            await self._change(SasState.KEYS_EXCHANGED)
        elif kind == "mac":
            if self.their_key is None or self.mac_received:
                raise CryptoError("Unexpected SAS MAC")
            self.request.check_snapshot()
            info = self._info(sending=False)
            macs = content["mac"]
            expected = self._mac(",".join(sorted(macs)), info + "KEY_IDS")
            if not hmac.compare_digest(expected, content["keys"]):
                await self.request.cancel("m.key_mismatch")
                return
            device_key = f"ed25519:{self.request.other_device_id}"
            if device_key not in macs:
                raise CryptoError("SAS device MAC is missing")
            known = dict(self.request._device_snapshot)
            identity = self.request.engine.devices.identity(self.request.other_user_id)
            master_id = None
            if identity:
                public = signing_key(
                    identity["master"], self.request.other_user_id, "master"
                )
                master_id = f"ed25519:{public}"
                known[master_id] = public
            for key, mac in macs.items():
                if key not in known or not hmac.compare_digest(
                    self._mac(known[key], info + key), mac
                ):
                    await self.request.cancel("m.key_mismatch")
                    return
            self.trust_identity = master_id in macs if master_id else False
            self.mac_received = True
            await self._finish()
        elif kind == "done":
            if not self.mac_received:
                raise CryptoError("SAS done before authenticated MAC")
            self.done_received = True
            await self._finish()


class QrVerification:
    def __init__(
        self, request: VerificationRequest, data: QrVerificationData, *, scanned: bool
    ) -> None:
        self.request, self.data = request, data
        self.scanned_by_us = scanned
        self.confirmed = False
        self.done_received = False
        self._state = Observable(
            QrVerificationState.RECIPROCATED if scanned else QrVerificationState.STARTED
        )

    def state(self) -> QrVerificationState:
        return self._state.value

    def changes(self) -> Any:
        return self._state.changes()

    def to_bytes(self) -> bytes:
        return self.data.to_bytes()

    def has_been_scanned(self) -> bool:
        return self.state() in {
            QrVerificationState.SCANNED,
            QrVerificationState.CONFIRMED,
            QrVerificationState.DONE,
        }

    def is_done(self) -> bool:
        return self.request.is_done()

    def is_cancelled(self) -> bool:
        return self.request.is_cancelled()

    async def confirm(self) -> None:
        if self.state() != QrVerificationState.SCANNED:
            return
        self.request.check_snapshot()
        await self.request.send("done", {})
        self.confirmed = True
        await self._change(QrVerificationState.CONFIRMED)
        if self.done_received:
            await self.finish()

    async def cancel(self) -> None:
        await self.request.cancel()

    async def _change(self, state: QrVerificationState) -> None:
        self._state.set(state)
        await self.request.engine.emit(
            "verification",
            {
                "flow_id": self.request.flow_id,
                "user_id": self.request.other_user_id,
                "state": state.value,
                "method": "m.reciprocate.v1",
            },
            handle=self,
        )

    async def finish(self) -> None:
        # A displayed trusted-master QR verifies the peer device; a scanned
        # trusted-master QR verifies our identity. Mode 2 has the inverse roles.
        trust = (
            self.data.mode == 0
            or (self.data.mode == 1 and self.scanned_by_us)
            or (self.data.mode == 2 and not self.scanned_by_us)
        )
        await self.request.complete(trust_identity=trust)
        await self._change(QrVerificationState.DONE)

    async def handle(self, kind: str, content: dict[str, Any]) -> None:
        if kind == "start":
            if (
                self.scanned_by_us
                or content.get("method") != "m.reciprocate.v1"
                or not hmac.compare_digest(b64d(content["secret"]), self.data.secret)
            ):
                raise CryptoError("QR reciprocation mismatch")
            await self._change(QrVerificationState.SCANNED)
        elif kind == "done":
            self.request.check_snapshot()
            self.done_received = True
            if self.scanned_by_us:
                await self.request.send("done", {})
                await self.finish()
            elif self.confirmed:
                await self.finish()
            else:
                raise CryptoError("QR done before scan confirmation")


class VerificationManager:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.requests: dict[tuple[str, str], VerificationRequest] = {}

    async def request(
        self,
        user_id: str,
        device_id: str,
        *,
        room_id: str | None = None,
        methods: list[str] | None = None,
    ) -> VerificationRequest:
        await self.engine.devices.query(
            self.engine, list({user_id, self.engine.user_id})
        )
        flow_id = uuid4().hex
        content = {"from_device": self.engine.device_id, "methods": methods or METHODS}
        if room_id:
            content.update(
                msgtype="m.key.verification.request",
                body="Verification request",
                to=user_id,
            )
            result = await self.engine.send_room_event(
                room_id, "m.room.message", content, flow_id
            )
            flow_id = str(result.event_id)
        else:
            content.update(transaction_id=flow_id, timestamp=int(time() * 1000))
            await self.engine.send_to_device(
                user_id, device_id, "m.key.verification.request", content
            )
        request = VerificationRequest(
            self, user_id, device_id, flow_id, room_id=room_id, we_started=True
        )
        request.our_methods = list(methods or METHODS)
        self.requests[user_id, flow_id] = request
        await request._change(VerificationRequestState.CREATED)
        return request

    async def expire(self) -> None:
        for request in list(self.requests.values()):
            if request.is_done() or request.is_cancelled():
                if monotonic() - request.created > 600:
                    self.requests.pop((request.other_user_id, request.flow_id), None)
                continue
            sas = request.verification
            expired = monotonic() - request.created > 600
            if isinstance(sas, SasVerification):
                expired |= (
                    monotonic() - sas.created > 300 or monotonic() - sas.last_event > 60
                )
            if expired:
                await request.cancel("m.timeout")

    async def handle(self, raw: Any, room_id: str | None = None) -> bool:
        content = raw.content
        is_request = raw.type == "m.key.verification.request" or (
            raw.type == "m.room.message"
            and content.get("msgtype") == "m.key.verification.request"
        )
        if not is_request and not raw.type.startswith("m.key.verification."):
            return False
        sender = str(raw.sender or "")
        if not sender:
            return True
        kind = "request" if is_request else raw.type.rsplit(".", 1)[-1]
        relation = content.get("m.relates_to", {})
        flow_id = (
            str(raw.event_id or "")
            if is_request and room_id
            else relation.get("event_id")
            if room_id
            else content.get("transaction_id")
        )
        if (
            not isinstance(flow_id, str)
            or not flow_id
            or (
                room_id and not is_request and relation.get("rel_type") != "m.reference"
            )
        ):
            return True
        request = self.requests.get((sender, flow_id))
        if is_request:
            if request:
                return True
            if room_id and content.get("to") != self.engine.user_id:
                return True
            timestamp = raw.origin_server_ts if room_id else content.get("timestamp")
            if (
                not isinstance(timestamp, (int, float))
                or time() * 1000 - timestamp > 600000
                or timestamp - time() * 1000 > 300000
            ):
                return True
            device_id = content.get("from_device")
            if (
                not isinstance(device_id, str)
                or device_id == "*"
                or (
                    sender == self.engine.user_id and device_id == self.engine.device_id
                )
            ):
                return True
            await self.engine.devices.query(
                self.engine, list({sender, self.engine.user_id})
            )
            request = VerificationRequest(
                self,
                sender,
                device_id,
                flow_id,
                room_id=room_id,
                methods=content.get("methods", []),
            )
            self.requests[sender, flow_id] = request
            request.snapshot()
            await request._change(VerificationRequestState.REQUESTED)
            if self.engine.adapter.matrix_config.matrix_auto_accept_verification:
                await request.accept()
            return True
        if (
            not request
            or request.room_id != room_id
            or request.is_done()
            or request.is_cancelled()
        ):
            return True
        marker = canonical_json({"kind": kind, "content": content})
        if marker in request._seen:
            return True
        if "from_device" in content and request.other_device_id not in (
            "*",
            content["from_device"],
        ):
            # Another device answered the same broadcast. It must not take over
            # a flow already bound to a device.
            return True
        try:
            if kind == "cancel":
                await request.cancel(content.get("code", "m.user"), received=True)
            elif kind == "ready":
                if (
                    not request.we_started
                    or request.state() != VerificationRequestState.CREATED
                    or not isinstance(content.get("from_device"), str)
                ):
                    raise CryptoError("Unexpected verification ready")
                request.other_device_id = content["from_device"]
                request.their_methods = content["methods"]
                request.snapshot()
                await request._change(VerificationRequestState.READY)
            elif kind == "start":
                if isinstance(request.verification, QrVerification):
                    await request.verification.handle(kind, content)
                elif content.get("method") == "m.sas.v1":
                    old = request.verification
                    if isinstance(old, SasVerification):
                        ours = (self.engine.user_id, self.engine.device_id)
                        theirs = (sender, request.other_device_id)
                        if not old.we_started or ours < theirs:
                            return True
                    elif not request.is_ready():
                        raise CryptoError("Verification start before ready")
                    request.snapshot()
                    sas = SasVerification(request, we_started=False)
                    sas.start_content = content
                    request.verification = sas
                    await request._change(VerificationRequestState.TRANSITIONED)
                    await sas.accept()
                else:
                    await request.cancel("m.unknown_method")
            elif request.verification:
                await request.verification.handle(kind, content)
            else:
                raise CryptoError("Unexpected verification event")
            request._seen.add(marker)
        except (CryptoError, ValueError, KeyError, TypeError, OlmSasError):
            await request.cancel("m.invalid_message")
        return True
