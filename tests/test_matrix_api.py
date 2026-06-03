import json
from urllib.parse import parse_qs, urlsplit

from nonebot.adapters.matrix.adapter import _parse_oauth_code
from nonebot.adapters.matrix.api.handle import quote_path
from nonebot.adapters.matrix.exception import RateLimitException, UnauthorizedException
from nonebot.adapters.matrix.oauth import (
    AuthorizationRequest,
    ClientRegistrationRequest,
    build_authorization_url,
    register_oauth_client,
)
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
async def test_refresh_token_request_builds_body(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"access_token":"new-token","refresh_token":"new-refresh","expires_in_ms":60000}'

    response = await dummy_bot.refresh_token(refresh_token="refresh-1")

    request = dummy_bot.adapter.request_calls[-1]
    assert str(request.url) == "https://matrix.example.org/_matrix/client/v3/refresh"
    assert request.json == {"refresh_token": "refresh-1"}
    assert response.access_token == "new-token"
    assert response.refresh_token == "new-refresh"
    assert response.expires_in_ms == 60000


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


def test_build_authorization_url_uses_fragment_and_msc2967_scope() -> None:
    url = build_authorization_url(
        AuthorizationRequest(
            authorization_endpoint="https://auth.example.org/authorize",
            client_id="client-123",
            redirect_uri="http://127.0.0.1:12345/callback",
            code_challenge="challenge",
            state="state-1",
            nonce="nonce-1",
            scope=(
                "openid "
                "urn:matrix:org.matrix.msc2967.client:api:* "
                "urn:matrix:org.matrix.msc2967.client:device:ABCDEFGHIJKL"
            ),
        ),
    )
    query = parse_qs(urlsplit(url).query)
    assert query["response_mode"] == ["fragment"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-123"]
    assert query["nonce"] == ["nonce-1"]
    assert (
        query["scope"][0] == "openid urn:matrix:org.matrix.msc2967.client:api:* "
        "urn:matrix:org.matrix.msc2967.client:device:ABCDEFGHIJKL"
    )


def test_parse_oauth_code_supports_query_and_fragment() -> None:
    assert (
        _parse_oauth_code("http://127.0.0.1/callback?code=query-code&state=abc")
        == "query-code"
    )
    assert (
        _parse_oauth_code("http://127.0.0.1/callback#code=fragment-code&state=abc")
        == "fragment-code"
    )
    assert (
        _parse_oauth_code("http://127.0.0.1/callback/fragment?code=bridge-code")
        == "bridge-code"
    )


@pytest.mark.asyncio
async def test_register_oauth_client_includes_client_uri() -> None:
    captured: dict[str, object] = {}

    async def post_json(
        url: str, data: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        captured["url"] = url
        captured["data"] = data
        return 200, {"client_id": "client-123", "redirect_uris": data["redirect_uris"]}

    registration = await register_oauth_client(
        ClientRegistrationRequest(
            registration_endpoint="https://account.matrix.org/oauth2/registration",
            client_name="Matrix Bot",
            redirect_uris=["http://127.0.0.1:39501/callback"],
            client_uri="https://matrix.org",
            application_type="native",
        ),
        http_post_json=post_json,
    )

    assert registration.client_id == "client-123"
    assert captured["url"] == "https://account.matrix.org/oauth2/registration"
    assert captured["data"] == {
        "application_type": "native",
        "client_name": "Matrix Bot",
        "redirect_uris": ["http://127.0.0.1:39501/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_uri": "https://matrix.org",
    }


@pytest.mark.asyncio
async def test_login_request_refresh_token_true(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = (
        b'{"user_id":"@bot:example.org","access_token":"new-token",'
        b'"refresh_token":"new-refresh","expires_in_ms":60000,'
        b'"device_id":"DEV-LOGIN"}'
    )

    response = await dummy_bot.login(
        password="secret",
        refresh_token=True,
    )

    request = dummy_bot.adapter.request_calls[-1]
    assert str(request.url) == "https://matrix.example.org/_matrix/client/v3/login"
    assert request.json["refresh_token"] is True
    assert request.json["password"] == "secret"
    assert request.json["type"] == "m.login.password"
    assert response.access_token == "new-token"
    assert response.refresh_token == "new-refresh"
    assert response.expires_in_ms == 60000
