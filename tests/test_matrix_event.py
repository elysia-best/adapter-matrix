from nonebot.adapters.matrix.api.model import RawMatrixEvent
from nonebot.adapters.matrix.event import (
    ReactionEvent,
    RoomMemberEvent,
    RoomMessageEvent,
    TypingEvent,
    UnknownRoomEvent,
    event_from_raw,
)


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
