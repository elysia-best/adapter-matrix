"""Client-Server API contract tests based on matrix-rust-sdk fixtures."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from nonebot.adapters.matrix.api.handle import quote_path
from nonebot.adapters.matrix.api.model import (
    RawMatrixEvent,
    RoomStateResponse,
    SyncResponse,
)
from nonebot.adapters.matrix.exception import ActionFailed, RateLimitException
from tests.fake.doubles import DummyAdapter, DummyBot

import pytest


def request_url(request: object) -> str:
    return str(request.url)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_parses_sdk_style_response_and_encodes_query(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.content = json.dumps(
        {
            "next_batch": "s725",
            "device_lists": {"changed": ["@alice:example.org"], "left": []},
            "device_one_time_keys_count": {"signed_curve25519": 47},
            "device_unused_fallback_key_types": ["signed_curve25519"],
            "to_device": {
                "events": [
                    {
                        "type": "m.room.encrypted",
                        "sender": "@alice:example.org",
                        "content": {"algorithm": "m.olm.v1.curve25519-aes-sha2"},
                    }
                ]
            },
            "rooms": {
                "join": {
                    "!room:example.org": {
                        "timeline": {
                            "limited": False,
                            "prev_batch": "s724",
                            "events": [
                                {
                                    "type": "m.room.message",
                                    "event_id": "$event:example.org",
                                    "sender": "@alice:example.org",
                                    "origin_server_ts": 1,
                                    "content": {"msgtype": "m.text", "body": "hi"},
                                }
                            ],
                        },
                        "state": {"events": []},
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                    }
                }
            },
        }
    ).encode()

    response = await dummy_adapter._api_sync(
        dummy_bot,
        since="s724",
        timeout=30_000,
        filter={"room": {"timeline": {"limit": 10}}},
        full_state=True,
        use_state_after=False,
    )

    assert isinstance(response, SyncResponse)
    assert response.next_batch == "s725"
    assert response.device_lists is not None
    assert response.device_lists.changed == ["@alice:example.org"]
    assert response.device_one_time_keys_count == {"signed_curve25519": 47}
    assert response.device_unused_fallback_key_types == ["signed_curve25519"]
    assert response.to_device is not None
    assert response.rooms.join["!room:example.org"].timeline.events[0].content[
        "body"
    ] == "hi"

    query = parse_qs(urlsplit(request_url(dummy_adapter.request_calls[-1])).query)
    assert query["since"] == ["s724"]
    assert query["timeout"] == ["30000"]
    assert query["full_state"] == ["true"]
    assert query["use_state_after"] == ["false"]
    assert json.loads(query["filter"][0]) == {"room": {"timeline": {"limit": 10}}}


@pytest.mark.asyncio
async def test_room_event_and_state_endpoints_use_encoded_paths(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.content = b'{"event_id":"$event:example.org"}'

    await dummy_adapter._api_send_event(
        dummy_bot,
        room_id="!room:example.org",
        event_type="m.room.message",
        txn_id="txn/1",
        content={"msgtype": "m.text", "body": "hello"},
    )
    request = dummy_adapter.request_calls[-1]
    assert request.method == "PUT"
    assert request_url(request).endswith(
        "/rooms/%21room%3Aexample.org/send/m.room.message/txn%2F1"
    )
    assert request.json == {"msgtype": "m.text", "body": "hello"}

    await dummy_adapter._api_send_state_event(
        dummy_bot,
        room_id="!room:example.org",
        event_type="m.room.topic",
        state_key="topic/one",
        txn_id="ignored",
        content={"topic": "Testing"},
    )
    request = dummy_adapter.request_calls[-1]
    assert request.method == "PUT"
    assert request_url(request).endswith(
        "/rooms/%21room%3Aexample.org/state/m.room.topic/topic%2Fone"
    )
    assert "/send/" not in request_url(request)


@pytest.mark.asyncio
async def test_room_lifecycle_and_state_reads_match_client_server_paths(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.responses = [
        (200, b'{"room_id":"!joined:example.org"}'),
        (200, b"{}"),
        (
            200,
            b'[{"type":"m.room.topic","state_key":"","content":{"topic":"x"}}]',
        ),
        (200, b'{"type":"m.room.topic","state_key":"","content":{"topic":"x"}}'),
    ]

    joined = await dummy_adapter._api_join_room(
        dummy_bot, room_id="#alias:example.org", reason="because"
    )
    assert joined.room_id == "!joined:example.org"
    assert request_url(dummy_adapter.request_calls[0]).endswith(
        "/join/%23alias%3Aexample.org"
    )
    assert dummy_adapter.request_calls[0].json == {"reason": "because"}

    await dummy_adapter._api_leave_room(dummy_bot, room_id="!joined:example.org")
    assert request_url(dummy_adapter.request_calls[1]).endswith(
        "/rooms/%21joined%3Aexample.org/leave"
    )
    assert dummy_adapter.request_calls[1].json == {}

    state = await dummy_adapter._api_get_room_state(
        dummy_bot, room_id="!joined:example.org"
    )
    assert isinstance(state, RoomStateResponse)
    assert state.events[0].content == {"topic": "x"}
    assert request_url(dummy_adapter.request_calls[2]).endswith(
        "/rooms/%21joined%3Aexample.org/state"
    )

    event = await dummy_adapter._api_get_room_state(
        dummy_bot,
        room_id="!joined:example.org",
        event_type="m.room.topic",
        state_key="",
    )
    assert isinstance(event, RawMatrixEvent)
    assert event.type == "m.room.topic"
    assert request_url(dummy_adapter.request_calls[3]).endswith(
        "/rooms/%21joined%3Aexample.org/state/m.room.topic/"
    )


@pytest.mark.asyncio
async def test_create_room_and_e2ee_key_endpoints_send_spec_payloads(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.responses = [
        (200, b'{"room_id":"!new:example.org"}'),
        (200, b'{"one_time_key_counts":{"signed_curve25519":2}}'),
        (200, b'{"device_keys":{}}'),
        (200, b'{"one_time_keys":{}}'),
    ]
    created = await dummy_adapter._api_create_room(
        dummy_bot,
        preset="private_chat",
        name="SDK test",
        invite=["@alice:example.org"],
        initial_state=[
            {"type": "m.room.encryption", "state_key": "", "content": {}}
        ],
        creation_content={"m.federate": False},
        is_direct=True,
    )
    assert created.room_id == "!new:example.org"
    assert dummy_adapter.request_calls[0].json == {
        "preset": "private_chat",
        "name": "SDK test",
        "invite": ["@alice:example.org"],
        "initial_state": [
            {"type": "m.room.encryption", "state_key": "", "content": {}}
        ],
        "creation_content": {"m.federate": False},
        "is_direct": True,
    }

    await dummy_adapter._api_keys_upload(
        dummy_bot,
        device_keys={"user_id": "@bot:example.org", "device_id": "DEV"},
        one_time_keys={"signed_curve25519:1": {"key": "abc"}},
    )
    assert dummy_adapter.request_calls[1].json == {
        "device_keys": {"user_id": "@bot:example.org", "device_id": "DEV"},
        "one_time_keys": {"signed_curve25519:1": {"key": "abc"}},
    }

    await dummy_adapter._api_keys_query(
        dummy_bot, device_keys={"@alice:example.org": []}, timeout=1_000
    )
    assert dummy_adapter.request_calls[2].json == {
        "device_keys": {"@alice:example.org": []},
        "timeout": 1_000,
    }

    await dummy_adapter._api_keys_claim(
        dummy_bot,
        one_time_keys={"@alice:example.org": {"ALICE": "signed_curve25519"}},
    )
    assert dummy_adapter.request_calls[3].json == {
        "one_time_keys": {"@alice:example.org": {"ALICE": "signed_curve25519"}}
    }


@pytest.mark.asyncio
async def test_to_device_backup_and_media_endpoints(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.responses = [
        (200, b"{}"),
        (200, b'{"version":"7","algorithm":"m.megolm_backup.v1"}'),
        (200, b'{"rooms":{}}'),
        (200, b"binary-data"),
        (200, b"thumbnail-data"),
    ]
    await dummy_adapter._api_send_to_device(
        dummy_bot,
        event_type="m.room.encrypted",
        txn_id="txn/2",
        messages={
            "@alice:example.org": {
                "ALICE": {"algorithm": "m.olm.v1.curve25519-aes-sha2"}
            }
        },
    )
    request = dummy_adapter.request_calls[0]
    assert request.method == "PUT"
    assert request_url(request).endswith(
        "/sendToDevice/m.room.encrypted/txn%2F2"
    )
    assert request.json["messages"]["@alice:example.org"]["ALICE"]["algorithm"]

    version = await dummy_adapter._api_room_keys_version(dummy_bot, version="7")
    assert version["algorithm"] == "m.megolm_backup.v1"
    assert request_url(dummy_adapter.request_calls[1]).endswith("/room_keys/version/7")

    keys = await dummy_adapter._api_room_keys_keys(
        dummy_bot,
        room_id="!room:example.org",
        session_id="sid/1",
        version="7",
    )
    assert keys == {"rooms": {}}
    query = parse_qs(urlsplit(request_url(dummy_adapter.request_calls[2])).query)
    assert query == {"version": ["7"]}
    assert request_url(dummy_adapter.request_calls[2]).endswith(
        "/room_keys/keys/%21room%3Aexample.org/sid%2F1?version=7"
    )

    media = await dummy_adapter._api_download_media(
        dummy_bot,
        server_name="example.org:8448",
        media_id="media/id",
        filename="file name.txt",
        allow_remote=True,
    )
    assert media == b"binary-data"
    assert request_url(dummy_adapter.request_calls[3]).endswith(
        "/download/example.org%3A8448/media%2Fid/file%20name.txt?allow_remote=true"
    )

    thumbnail = await dummy_adapter._api_thumbnail_media(
        dummy_bot,
        server_name="example.org",
        media_id="media",
        width=100,
        height=80,
        method="crop",
        allow_remote=False,
    )
    assert thumbnail == b"thumbnail-data"
    thumbnail_query = parse_qs(
        urlsplit(request_url(dummy_adapter.request_calls[4])).query
    )
    assert thumbnail_query == {
        "width": ["100"],
        "height": ["80"],
        "method": ["crop"],
        "allow_remote": ["false"],
    }


@pytest.mark.asyncio
async def test_room_messages_relations_receipts_and_typing(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.responses = [
        (200, b'{"chunk":[],"start":"s1","end":"s2","state":[]}'),
        (200, b'{"chunk":[],"next_batch":"n"}'),
        (200, b"{}"),
        (200, b"{}"),
    ]
    messages = await dummy_adapter._api_get_room_messages(
        dummy_bot,
        room_id="!room:example.org",
        from_token="s0",
        dir="b",
        limit=20,
        filter={"contains_url": True},
    )
    assert messages.start == "s1"
    query = parse_qs(urlsplit(request_url(dummy_adapter.request_calls[0])).query)
    assert query["from"] == ["s0"]
    assert query["dir"] == ["b"]
    assert query["limit"] == ["20"]
    assert json.loads(query["filter"][0]) == {"contains_url": True}

    relations = await dummy_adapter._api_get_relations(
        dummy_bot,
        room_id="!room:example.org",
        event_id="$event:example.org",
        rel_type="m.annotation",
        event_type="m.reaction",
        from_token="r0",
    )
    assert relations.next_batch == "n"
    assert request_url(dummy_adapter.request_calls[1]).endswith(
        "/rooms/%21room%3Aexample.org/relations/%24event%3Aexample.org/"
        "m.annotation/m.reaction?from=r0"
    )

    await dummy_adapter._api_set_typing(
        dummy_bot,
        room_id="!room:example.org",
        user_id="@bot:example.org",
        typing=True,
        timeout=3_000,
    )
    assert dummy_adapter.request_calls[2].json == {"typing": True, "timeout": 3_000}

    await dummy_adapter._api_post_receipt(
        dummy_bot,
        room_id="!room:example.org",
        receipt_type="m.read",
        event_id="$event:example.org",
        thread_id="main",
    )
    assert dummy_adapter.request_calls[3].json == {"thread_id": "main"}


@pytest.mark.asyncio
async def test_error_payloads_are_preserved_for_client_server_failures(
    dummy_adapter: DummyAdapter,
    dummy_bot: DummyBot,
) -> None:
    dummy_adapter.status_code = 403
    dummy_adapter.content = b'{"errcode":"M_FORBIDDEN","error":"denied"}'
    with pytest.raises(ActionFailed) as forbidden:
        await dummy_adapter._api_whoami(dummy_bot)
    assert forbidden.value.errcode == "M_FORBIDDEN"
    assert forbidden.value.message == "denied"

    dummy_adapter.status_code = 429
    dummy_adapter.content = b'{"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":2500}'
    with pytest.raises(RateLimitException) as limited:
        await dummy_adapter._api_whoami(dummy_bot)
    assert limited.value.retry_after_ms == 2500


def test_quote_path_matches_matrix_identifier_encoding() -> None:
    assert quote_path("!room:example.org") == "%21room%3Aexample.org"
    assert quote_path("$event:example.org") == "%24event%3Aexample.org"
    assert quote_path("txn/1") == "txn%2F1"
