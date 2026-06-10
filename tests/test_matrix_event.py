import json

from nonebot.adapters.matrix.api.model import RawMatrixEvent, SyncResponse
from nonebot.adapters.matrix.event import (
    ReactionEvent,
    RoomMemberEvent,
    RoomMessageEvent,
    TypingEvent,
    UnknownRoomEvent,
    event_from_raw,
)

from nonebot.compat import type_validate_python
from nonebot.log import logger


def test_room_message_event_converts_message_and_session() -> None:
    raw = RawMatrixEvent(
        type="m.room.message",
        event_id="$event:example.org",
        sender="@alice:example.org",
        content={"msgtype": "m.text", "body": "hello"},
    )

    event = event_from_raw(raw, room_id="!room:example.org", to_me=True)

    assert isinstance(event, RoomMessageEvent)
    assert event.get_type() == "message"
    assert event.get_user_id() == "@alice:example.org"
    assert event.get_session_id() == "!room:example.org"
    assert event.get_message().extract_plain_text() == "hello"
    assert event.is_tome()


def test_notice_event_variants() -> None:
    member = event_from_raw(
        RawMatrixEvent(
            type="m.room.member",
            sender="@alice:example.org",
            state_key="@bob:example.org",
            content={"membership": "join"},
        ),
        room_id="!room:example.org",
    )
    assert isinstance(member, RoomMemberEvent)
    assert member.membership == "join"

    reaction = event_from_raw(
        RawMatrixEvent(
            type="m.reaction",
            sender="@alice:example.org",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target:example.org",
                    "key": "👍",
                }
            },
        ),
        room_id="!room:example.org",
    )
    assert isinstance(reaction, ReactionEvent)
    assert reaction.target_event_id == "$target:example.org"
    assert reaction.key == "👍"

    typing = event_from_raw(
        RawMatrixEvent(type="m.typing", content={"user_ids": ["@alice:example.org"]}),
        room_id="!room:example.org",
    )
    assert isinstance(typing, TypingEvent)
    assert typing.user_ids == ["@alice:example.org"]

    unknown = event_from_raw(
        RawMatrixEvent(type="com.example.custom", content={"body": "raw"}),
        room_id="!room:example.org",
    )
    assert isinstance(unknown, UnknownRoomEvent)


def test_room_message_event_with_html_tags_in_body() -> None:
    """Messages with HTML tags in body should be parsed as plain text."""
    raw = RawMatrixEvent(
        type="m.room.message",
        event_id="$event:example.org",
        sender="@alice:example.org",
        content={
            "msgtype": "m.text",
            "body": "<html><h1>标题</h1></html>\n",
            "m.mentions": {},
        },
    )

    event = event_from_raw(raw, room_id="!room:example.org")

    assert isinstance(event, RoomMessageEvent)
    assert event.get_type() == "message"
    assert event.get_message().extract_plain_text() == "<html><h1>标题</h1></html>\n"


def test_room_message_event_escapes_html_tags_for_colored_logs() -> None:
    raw = RawMatrixEvent(
        type="m.room.message",
        event_id="$event:example.org",
        sender="@alice:example.org",
        content={
            "msgtype": "m.text",
            "body": "<html><h1>content</h1></html>",
            "format": "org.matrix.custom.html",
            "formatted_body": "&lt;html&gt;&lt;h1&gt;content&lt;/h1&gt;&lt;/html&gt;",
            "m.mentions": {},
        },
    )

    event = event_from_raw(raw, room_id="!room:example.org")

    assert isinstance(event, RoomMessageEvent)
    assert event.get_message().extract_plain_text() == "<html><h1>content</h1></html>"
    log_string = event.get_log_string()
    assert r"\<html>\<h1>content\</h1>\</html>" in log_string
    logger.opt(colors=True).success(log_string)


def test_model_validate_from_real_api_response() -> None:
    """Test that model_validate works on data from a real /sync API response.

    This reproduces the exact path in _dispatch_room_event:
    1. Parse SyncResponse from raw JSON
    2. Extract RawMatrixEvent from timeline
    3. raw.model_dump(by_alias=True, exclude_none=True)
    4. RoomMessageEvent.model_validate(data)
    """
    body = json.dumps(
        {
            "next_batch": "m7055416435~37",
            "rooms": {
                "join": {
                    "!PPaIWnOCgdZmRMfpXH:matrix.debian.social": {
                        "timeline": {
                            "events": [
                                {
                                    "unsigned": {"membership": "join"},
                                    "content": {
                                        "body": "<html><h1>标题</h1></html>\n",
                                        "m.mentions": {},
                                        "msgtype": "m.text",
                                    },
                                    "origin_server_ts": 1781087180630,
                                    "sender": "@bob:example.com",
                                    "type": "m.room.message",
                                    "event_id": "$ioCkmKP0FCHFezcIEZhjLTnzxwpnuGkP-Dseu736_hk",
                                }
                            ],
                            "prev_batch": "s7055416438",
                            "limited": False,
                        },
                        "state": {"events": []},
                        "account_data": {"events": []},
                        "ephemeral": {"events": []},
                        "unread_notifications": {
                            "notification_count": 47,
                            "highlight_count": 0,
                        },
                        "summary": {},
                    }
                }
            },
        }
    )

    # Step 1: Parse the full sync response
    sync = type_validate_python(SyncResponse, json.loads(body))
    assert sync.next_batch == "m7055416435~37"

    # Step 2: Extract the RawMatrixEvent from timeline
    room = sync.rooms.join["!PPaIWnOCgdZmRMfpXH:matrix.debian.social"]
    raw = room.timeline.events[0]
    assert raw.type == "m.room.message"
    assert raw.event_id == "$ioCkmKP0FCHFezcIEZhjLTnzxwpnuGkP-Dseu736_hk"
    assert raw.sender == "@bob:example.com"
    assert raw.unsigned is not None
    assert raw.unsigned.membership == "join"

    # Step 3: model_dump (exactly as in event_from_raw)
    data = raw.model_dump(by_alias=True, exclude_none=True)
    data["room_id"] = "!PPaIWnOCgdZmRMfpXH:matrix.debian.social"
    data["to_me"] = False

    # Step 4: model_validate (the line that was suspected to hang)
    event = RoomMessageEvent.model_validate(data)

    # Verify the result
    assert isinstance(event, RoomMessageEvent)
    assert event.room_id == "!PPaIWnOCgdZmRMfpXH:matrix.debian.social"
    assert event.sender == "@bob:example.com"
    assert event.content["msgtype"] == "m.text"
    assert event.to_me is False
    msg = event.get_message()
    assert msg.extract_plain_text() == "<html><h1>标题</h1></html>\n"


def test_event_from_raw_via_full_pipeline() -> None:
    """Test event_from_raw with raw event from real API response (unsigned present)."""
    body = json.dumps(
        {
            "next_batch": "m7055416435~37",
            "rooms": {
                "join": {
                    "!PPaIWnOCgdZmRMfpXH:matrix.debian.social": {
                        "timeline": {
                            "events": [
                                {
                                    "unsigned": {"membership": "join"},
                                    "content": {
                                        "body": "<html><h1>标题</h1></html>\n",
                                        "m.mentions": {},
                                        "msgtype": "m.text",
                                    },
                                    "origin_server_ts": 1781087180630,
                                    "sender": "@bob:example.com",
                                    "type": "m.room.message",
                                    "event_id": "$ioCkmKP0FCHFezcIEZhjLTnzxwpnuGkP-Dseu736_hk",
                                }
                            ],
                            "prev_batch": "s",
                            "limited": False,
                        },
                        "state": {"events": []},
                        "account_data": {"events": []},
                        "ephemeral": {"events": []},
                        "unread_notifications": {},
                        "summary": {},
                    }
                }
            },
        }
    )

    sync = type_validate_python(SyncResponse, json.loads(body))
    raw = sync.rooms.join["!PPaIWnOCgdZmRMfpXH:matrix.debian.social"].timeline.events[0]

    event = event_from_raw(
        raw, room_id="!PPaIWnOCgdZmRMfpXH:matrix.debian.social", to_me=False
    )

    assert isinstance(event, RoomMessageEvent)
    assert event.get_type() == "message"
    assert event.get_message().extract_plain_text() == "<html><h1>标题</h1></html>\n"
