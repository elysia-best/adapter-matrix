"""Engine-level E2EE tests: room key share tolerance and m.no_olm recovery."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from nonebot.adapters.matrix.api.model import RawMatrixEvent
from nonebot.adapters.matrix.api.types import UserId
from nonebot.adapters.matrix.crypto import CryptoEngine
from nonebot.adapters.matrix.crypto.primitives import (
    MEGOLM_ALGORITHM,
    OLM_ALGORITHM,
    canonical_json,
    sign_json,
)
from tests.fake.doubles import DummyAdapter, DummyBot

from olm.account import Account
import pytest


def make_device(user_id: str, device_id: str) -> tuple[Account, dict[str, Any]]:
    account = Account()
    keys = account.identity_keys
    device_keys = sign_json(
        {
            "user_id": user_id,
            "device_id": device_id,
            "algorithms": [OLM_ALGORITHM, MEGOLM_ALGORITHM],
            "keys": {
                f"curve25519:{device_id}": keys["curve25519"],
                f"ed25519:{device_id}": keys["ed25519"],
            },
        },
        account,
        user_id,
        device_id,
    )
    return account, device_keys


def make_engine() -> tuple[DummyAdapter, CryptoEngine]:
    adapter = DummyAdapter()
    bot = DummyBot(adapter, device_id="BOT")
    engine = CryptoEngine(bot, adapter)
    engine.account.load_or_create()
    return adapter, engine


def otk_response(account: Account, user_id: str, device_id: str) -> dict[str, Any]:
    account.generate_one_time_keys(1)
    key_id, key = next(iter(account.one_time_keys["curve25519"].items()))
    return {
        "one_time_keys": {
            user_id: {
                device_id: {
                    f"signed_curve25519:{key_id}": sign_json(
                        {"key": key}, account, user_id, device_id
                    )
                }
            }
        }
    }


def to_device_payloads(adapter: DummyAdapter) -> list[dict[str, Any]]:
    payloads = []
    for call in adapter.request_calls:
        if "/sendToDevice/" not in str(call.url):
            continue
        payload = call.json
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


@pytest.mark.asyncio
async def test_share_room_skips_unclaimable_device() -> None:
    adapter, engine = make_engine()
    peer, peer_keys = make_device("@peer:example.org", "PEER")
    _, dead_keys = make_device("@peer:example.org", "DEAD")

    members = {
        "chunk": [
            {
                "type": "m.room.member",
                "state_key": "@bot:example.org",
                "content": {"membership": "join"},
            },
            {
                "type": "m.room.member",
                "state_key": "@peer:example.org",
                "content": {"membership": "join"},
            },
        ]
    }
    device_keys = {
        "device_keys": {
            "@bot:example.org": {"BOT": engine.account.build_device_keys(engine.bot)},
            "@peer:example.org": {"PEER": peer_keys, "DEAD": dead_keys},
        }
    }
    adapter.responses = [
        (200, canonical_json(members).encode()),
        # json.dumps keeps the insertion order so PEER is processed before
        # the unclaimable DEAD device.
        (200, json.dumps(device_keys).encode()),
        (
            200,
            canonical_json(otk_response(peer, "@peer:example.org", "PEER")).encode(),
        ),
    ]

    _, record = await engine._share_room(
        "!room:example.org",
        {"algorithm": MEGOLM_ALGORITHM, "history_visibility": "shared"},
    )

    assert any(key.startswith("@peer:example.org|PEER|") for key in record["shared"])
    assert not any(
        key.startswith("@peer:example.org|DEAD|") for key in record["shared"]
    )

    payloads = to_device_payloads(adapter)
    inside = [payload["messages"]["@peer:example.org"] for payload in payloads]
    peer_messages = [item["PEER"] for item in inside if "PEER" in item]
    dead_messages = [item["DEAD"] for item in inside if "DEAD" in item]
    assert len(peer_messages) == 1
    assert peer_messages[0]["algorithm"] == OLM_ALGORITHM
    assert len(dead_messages) == 1
    assert dead_messages[0]["code"] == "m.no_olm"
    assert dead_messages[0]["from_device"] == "BOT"

    await engine.close()


@pytest.mark.asyncio
async def test_recover_no_olm_establishes_session_and_requests_keys() -> None:
    adapter, engine = make_engine()
    peer, peer_keys = make_device("@peer:example.org", "PEER")
    curve = peer.identity_keys["curve25519"]

    async def ignore_event(_event: object) -> None:
        return

    engine.bot.handle_event = ignore_event
    engine.store.put(
        "devices",
        {"@peer:example.org": {"PEER": {"keys": peer_keys, "local_trust": "Unset"}}},
    )
    engine.store.put(
        "undecrypted/$event-1",
        {
            "room_id": "!room:example.org",
            "event": {
                "sender": "@peer:example.org",
                "content": {
                    "algorithm": MEGOLM_ALGORITHM,
                    "session_id": "sid-1",
                    "sender_key": curve,
                },
            },
        },
    )
    adapter.responses = [
        (200, b"{}"),
        (
            200,
            canonical_json(otk_response(peer, "@peer:example.org", "PEER")).encode(),
        ),
    ]

    event = RawMatrixEvent(
        type="m.room_key.withheld",
        sender=UserId("@peer:example.org"),
        content={"algorithm": MEGOLM_ALGORITHM, "code": "m.no_olm"},
    )
    await engine.handle_to_device_event(event)
    await asyncio.sleep(0)

    assert engine.store.get("no_olm_recovery/@peer:example.org")

    payloads = to_device_payloads(adapter)
    inside = [payload["messages"]["@peer:example.org"] for payload in payloads]
    assert any(
        "PEER" in item and item["PEER"]["algorithm"] == OLM_ALGORITHM for item in inside
    )
    requests = [item["*"] for item in inside if "*" in item]
    assert len(requests) == 1
    assert requests[0]["action"] == "request"
    assert requests[0]["body"]["session_id"] == "sid-1"
    assert requests[0]["body"]["sender_key"] == curve

    before = len(adapter.request_calls)
    await engine.handle_to_device_event(event)
    await asyncio.sleep(0)
    assert len(adapter.request_calls) == before

    await engine.close()
