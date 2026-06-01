import pytest

from nonebot.adapters.matrix.api.model import RawMatrixEvent
from nonebot.adapters.matrix.config import Config
from nonebot.adapters.matrix.event import Event
from tests.fake.doubles import DummyBot


@pytest.mark.asyncio
async def test_dispatch_room_event_discards_old_events_by_default(
    dummy_bot: DummyBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_events: list[Event] = []

    async def handle_event(event: Event) -> None:
        handled_events.append(event)

    monkeypatch.setattr(dummy_bot, "handle_event", handle_event)
    dummy_bot.startup_time_ms = 1_000

    await dummy_bot.adapter._dispatch_room_event(
        dummy_bot,
        RawMatrixEvent(
            type="m.room.message",
            sender="@alice:example.org",
            origin_server_ts=999,
            content={"msgtype": "m.text", "body": "old"},
        ),
        room_id="!room:example.org",
    )
    await dummy_bot.adapter._dispatch_room_event(
        dummy_bot,
        RawMatrixEvent(
            type="m.room.message",
            sender="@alice:example.org",
            origin_server_ts=1_001,
            content={"msgtype": "m.text", "body": "new"},
        ),
        room_id="!room:example.org",
    )

    assert [event.get_message().extract_plain_text() for event in handled_events] == ["new"]


@pytest.mark.asyncio
async def test_dispatch_room_event_handles_old_events_when_configured(
    dummy_bot: DummyBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled_events: list[Event] = []

    async def handle_event(event: Event) -> None:
        handled_events.append(event)

    monkeypatch.setattr(dummy_bot, "handle_event", handle_event)
    dummy_bot.adapter.matrix_config.matrix_handle_old_events = True
    dummy_bot.startup_time_ms = 1_000

    await dummy_bot.adapter._dispatch_room_event(
        dummy_bot,
        RawMatrixEvent(
            type="m.room.message",
            sender="@alice:example.org",
            origin_server_ts=999,
            content={"msgtype": "m.text", "body": "old"},
        ),
        room_id="!room:example.org",
    )

    assert [event.get_message().extract_plain_text() for event in handled_events] == ["old"]


def test_old_event_handling_is_disabled_by_default() -> None:
    assert Config().matrix_handle_old_events is False
