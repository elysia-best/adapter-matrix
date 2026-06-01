import json
from urllib.parse import parse_qs, urlsplit

from nonebot.adapters.matrix.api.handle import quote_path
from nonebot.adapters.matrix.exception import RateLimitException, UnauthorizedException
from tests.fake.doubles import DummyBot

import pytest


@pytest.mark.asyncio
async def test_whoami_request_uses_bearer_token(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"user_id":"@bot:example.org","device_id":"DEV"}'

    response = await dummy_bot.whoami()

    request = dummy_bot.adapter.request_calls[-1]
    assert (
        str(request.url)
        == "https://matrix.example.org/_matrix/client/v3/account/whoami"
    )
    assert request.headers["Authorization"] == "Bearer test-token"
    assert response.user_id == "@bot:example.org"
    assert response.device_id == "DEV"


@pytest.mark.asyncio
async def test_sync_request_builds_query(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"next_batch":"s1"}'

    await dummy_bot.sync(
        since="s0",
        timeout=30000,
        filter={"room": {"timeline": {"limit": 10}}},
        set_presence="online",
    )

    request = dummy_bot.adapter.request_calls[-1]
    assert str(request.url).startswith(
        "https://matrix.example.org/_matrix/client/v3/sync"
    )
    query = parse_qs(urlsplit(str(request.url)).query)
    assert query["since"] == ["s0"]
    assert query["timeout"] == ["30000"]
    assert json.loads(query["filter"][0])["room"]["timeline"]["limit"] == 10
    assert query["set_presence"] == ["online"]


@pytest.mark.asyncio
async def test_send_event_url_encodes_matrix_ids(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"event_id":"$sent:example.org"}'

    await dummy_bot.send_event(
        room_id="!room:example.org",
        event_type="m.room.message",
        txn_id="txn/1",
        content={"msgtype": "m.text", "body": "hello"},
    )

    request = dummy_bot.adapter.request_calls[-1]
    assert str(request.url) == (
        "https://matrix.example.org/_matrix/client/v3/rooms/"
        "%21room%3Aexample.org/send/m.room.message/txn%2F1"
    )
    assert request.json == {"msgtype": "m.text", "body": "hello"}


@pytest.mark.asyncio
async def test_media_upload_uses_raw_content(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"content_uri":"mxc://example.org/media"}'

    response = await dummy_bot.upload_content(
        content=b"data",
        filename="a.txt",
        content_type="text/plain",
    )

    request = dummy_bot.adapter.request_calls[-1]
    assert str(request.url).startswith(
        "https://matrix.example.org/_matrix/media/v3/upload"
    )
    assert parse_qs(urlsplit(str(request.url)).query) == {"filename": ["a.txt"]}
    assert request.headers["Content-Type"] == "text/plain"
    assert request.content == b"data"
    assert response.content_uri == "mxc://example.org/media"


@pytest.mark.asyncio
async def test_matrix_error_mapping(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.status_code = 401
    dummy_bot.adapter.content = b'{"errcode":"M_UNKNOWN_TOKEN","error":"bad token"}'

    with pytest.raises(UnauthorizedException):
        await dummy_bot.whoami()

    dummy_bot.adapter.status_code = 429
    dummy_bot.adapter.content = b'{"errcode":"M_LIMIT_EXCEEDED","retry_after_ms":1000}'

    with pytest.raises(RateLimitException) as exception:
        await dummy_bot.whoami()
    assert exception.value.retry_after_ms == 1000


def test_quote_path_escapes_reserved_characters() -> None:
    assert quote_path("!room:example.org") == "%21room%3Aexample.org"
    assert quote_path("txn/1") == "txn%2F1"
