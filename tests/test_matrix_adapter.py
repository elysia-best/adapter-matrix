import asyncio
import json
from pathlib import Path
from time import time

from nonebot.adapters.matrix.api.model import RawMatrixEvent
from nonebot.adapters.matrix.config import BotInfo, Config
from nonebot.adapters.matrix.event import Event
from tests.fake.doubles import DummyAdapter, DummyBot

import pytest


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

    assert [event.get_message().extract_plain_text() for event in handled_events] == [
        "new"
    ]


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

    assert [event.get_message().extract_plain_text() for event in handled_events] == [
        "old"
    ]


def test_old_event_handling_is_disabled_by_default() -> None:
    assert Config().matrix_handle_old_events is False


@pytest.mark.asyncio
async def test_bootstrap_loads_persisted_tokens(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    key = json.dumps(
        {
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    store_path.write_text(
        json.dumps(
            {
                key: {
                    "access_token": "persisted-token",
                    "refresh_token": "persisted-refresh",
                    "access_token_expires_at_ms": 12345,
                    "device_id": "DEV1",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[(200, b'{"user_id":"@bot:example.org","device_id":"DEV1"}')],
    )
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="static-token",
        user_id="@bot:example.org",
    )

    self_info = await adapter._bootstrap_bot(bot_info)

    assert bot_info.access_token == "persisted-token"
    assert bot_info.refresh_token == "persisted-refresh"
    assert bot_info.access_token_expires_at_ms == 12345
    assert self_info.device_id == "DEV1"
    assert adapter.request_calls[0].headers["Authorization"] == "Bearer persisted-token"


@pytest.mark.asyncio
async def test_bootstrap_refreshes_after_unauthorized(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
            (
                200,
                b'{"access_token":"new-token","refresh_token":"new-refresh","expires_in_ms":60000}',
            ),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV2"}'),
        ],
    )
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="old-token",
        refresh_token="old-refresh",
        user_id="@bot:example.org",
    )

    self_info = await adapter._bootstrap_bot(bot_info)

    assert self_info.device_id == "DEV2"
    assert bot_info.access_token == "new-token"
    assert bot_info.refresh_token == "new-refresh"
    assert bot_info.access_token_expires_at_ms is not None
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert next(iter(payload.values()))["access_token"] == "new-token"


@pytest.mark.asyncio
async def test_sync_loop_refreshes_before_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DummyAdapter(
        responses=[
            (
                200,
                b'{"access_token":"new-token","refresh_token":"new-refresh","expires_in_ms":60000}',
            ),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV3"}'),
            (200, b'{"next_batch":"s1"}'),
        ]
    )
    bot = DummyBot(
        adapter,
        access_token="old-token",
        refresh_token="old-refresh",
        access_token_expires_at_ms=int(time() * 1000),
        refresh_before_expiry_ms=1000,
    )

    async def handle_sync(bot_: DummyBot, sync: object) -> None:
        raise asyncio.CancelledError

    async def fail_sleep(delay: float) -> None:
        msg = f"unexpected retry sleep: {delay}"
        raise AssertionError(msg)

    monkeypatch.setattr(adapter, "_handle_sync", handle_sync)
    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fail_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    assert bot.bot_info.access_token == "new-token"
    assert adapter.request_calls[0].url.path.endswith("/refresh")
    assert adapter.request_calls[2].headers["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_sync_loop_refreshes_after_unknown_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DummyAdapter(
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
            (
                200,
                b'{"access_token":"new-token","refresh_token":"new-refresh","expires_in_ms":60000}',
            ),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV4"}'),
            (200, b'{"next_batch":"s2"}'),
        ]
    )
    bot = DummyBot(adapter, access_token="old-token", refresh_token="old-refresh")

    async def handle_sync(bot_: DummyBot, sync: object) -> None:
        raise asyncio.CancelledError

    async def fail_sleep(delay: float) -> None:
        msg = f"unexpected retry sleep: {delay}"
        raise AssertionError(msg)

    monkeypatch.setattr(adapter, "_handle_sync", handle_sync)
    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fail_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    assert bot.bot_info.access_token == "new-token"
    assert adapter.request_calls[1].url.path.endswith("/refresh")
    assert adapter.request_calls[3].headers["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_sync_loop_without_refresh_token_keeps_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DummyAdapter(
        responses=[(401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}')]
    )
    bot = DummyBot(adapter, access_token="old-token")
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    assert len(adapter.request_calls) == 1
    assert sleeps == [adapter.matrix_config.matrix_retry_interval]


@pytest.mark.asyncio
async def test_load_invalid_token_store_ignores_broken_json(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    store_path.write_text("not json", encoding="utf-8")
    adapter = DummyAdapter(token_store_path=str(store_path))
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="static-token",
        user_id="@bot:example.org",
    )

    adapter._load_persisted_tokens(bot_info)

    assert bot_info.access_token == "static-token"


@pytest.mark.asyncio
async def test_bootstrap_logins_with_password(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
            (
                200,
                b'{"user_id":"@bot:example.org","access_token":"login-token",'
                b'"refresh_token":"login-refresh","expires_in_ms":60000,'
                b'"device_id":"DEV-LOGIN"}',
            ),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV-LOGIN"}'),
        ],
    )
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="old-token",
        login_user="@bot:example.org",
        login_password="secret",
        user_id="@bot:example.org",
    )

    self_info = await adapter._bootstrap_bot(bot_info)

    assert self_info.user_id == "@bot:example.org"
    assert bot_info.access_token == "login-token"
    assert bot_info.refresh_token == "login-refresh"
    assert bot_info.session_type == "legacy_login"
    assert bot_info.access_token_expires_at_ms is not None
    login_call = adapter.request_calls[1]
    assert str(login_call.url).endswith("/login")
    assert login_call.json["refresh_token"] is True
    assert login_call.json["password"] == "secret"


@pytest.mark.asyncio
async def test_bootstrap_oauth2_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nonebot.adapters.matrix.oauth import OAuth2TokenResponse

    async def mock_oauth_login(bot_info: BotInfo) -> OAuth2TokenResponse:
        bot_info.oauth_token_endpoint = "https://auth.example.org/token"
        return OAuth2TokenResponse(
            access_token="oauth-token",
            refresh_token="oauth-refresh",
            expires_in=300,
        )

    store_path = tmp_path / "tokens.json"
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV-OAUTH"}'),
        ],
    )
    monkeypatch.setattr(adapter, "_run_oauth2_login", mock_oauth_login)

    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="old-token",
        oauth_enabled=True,
        oauth_client_id="client-123",
        user_id="@bot:example.org",
    )

    self_info = await adapter._bootstrap_bot(bot_info)

    assert self_info.user_id == "@bot:example.org"
    assert bot_info.access_token == "oauth-token"
    assert bot_info.refresh_token == "oauth-refresh"
    assert bot_info.session_type == "oauth2"
    assert bot_info.access_token_expires_at_ms is not None


@pytest.mark.asyncio
async def test_soft_logout_triggers_relogin(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DummyAdapter(
        responses=[
            (
                401,
                b'{"errcode":"M_UNKNOWN_TOKEN","error":"soft logout","soft_logout":true}',
            ),
            (
                401,
                b'{"errcode":"M_UNKNOWN_TOKEN","error":"soft logout","soft_logout":true}',
            ),
            (
                200,
                b'{"user_id":"@bot:example.org","access_token":"relogin-token",'
                b'"refresh_token":"relogin-refresh","expires_in_ms":60000,'
                b'"device_id":"DEV-RELOGIN"}',
            ),
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV-RELOGIN"}'),
            (200, b'{"next_batch":"s3"}'),
        ],
    )
    bot = DummyBot(
        adapter,
        access_token="old-token",
        refresh_token="old-refresh",
        access_token_expires_at_ms=int(time() * 1000) + 60000,
    )
    bot.bot_info.login_password = "secret"
    bot.bot_info.login_user = "@bot:example.org"
    bot.bot_info.session_type = "legacy_login"

    async def handle_sync(bot_: DummyBot, sync: object) -> None:
        raise asyncio.CancelledError

    async def fail_sleep(delay: float) -> None:
        msg = f"unexpected retry sleep: {delay}"
        raise AssertionError(msg)

    monkeypatch.setattr(adapter, "_handle_sync", handle_sync)
    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fail_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    assert bot.bot_info.access_token == "relogin-token"
    assert bot.bot_info.refresh_token == "relogin-refresh"


@pytest.mark.asyncio
async def test_refresh_5xx_keeps_token(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DummyAdapter(
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
            (500, b'{"error":"internal server error"}'),
        ],
    )
    bot = DummyBot(
        adapter,
        access_token="old-token",
        refresh_token="old-refresh",
    )

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    assert bot.bot_info.refresh_token == "old-refresh"
    assert bot.bot_info.access_token == "old-token"
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_soft_logout_no_credentials_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = DummyAdapter(
        responses=[
            (
                401,
                b'{"errcode":"M_UNKNOWN_TOKEN","error":"soft logout","soft_logout":true}',
            ),
            (
                401,
                b'{"errcode":"M_UNKNOWN_TOKEN","error":"refresh denied","soft_logout":true}',
            ),
        ],
    )
    bot = DummyBot(
        adapter,
        access_token="old-token",
        refresh_token="old-refresh",
    )
    bot.bot_info.session_type = "legacy_login"

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("nonebot.adapters.matrix.adapter.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_loop(bot)

    # No credentials, should sleep/retry rather than relogin
    assert len(sleeps) == 1
    # Token should not have changed
    assert bot.bot_info.access_token == "old-token"


@pytest.mark.asyncio
async def test_session_type_persistence(tmp_path: Path) -> None:
    store_path = tmp_path / "tokens.json"
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV1"}'),
        ],
    )
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="test-token",
        refresh_token="test-refresh",
        user_id="@bot:example.org",
        session_type="legacy_login",
    )

    await adapter._bootstrap_bot(bot_info)

    payload = json.loads(store_path.read_text(encoding="utf-8"))
    entry = next(iter(payload.values()))
    assert entry["session_type"] == "legacy_login"

    adapter2 = DummyAdapter(
        token_store_path=str(store_path),
        responses=[(200, b'{"user_id":"@bot:example.org","device_id":"DEV1"}')],
    )
    bot_info2 = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="different-token",
        user_id="@bot:example.org",
    )
    adapter2._load_persisted_tokens(bot_info2)
    assert bot_info2.session_type == "legacy_login"
    assert bot_info2.access_token == "test-token"
    assert bot_info2.refresh_token == "test-refresh"


@pytest.mark.asyncio
async def test_static_token_no_auto_refresh() -> None:
    adapter = DummyAdapter(
        responses=[
            (401, b'{"errcode":"M_UNKNOWN_TOKEN","error":"expired"}'),
        ],
    )
    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="old-token",
        user_id="@bot:example.org",
    )

    with pytest.raises(RuntimeError, match="Failed to bootstrap"):
        await adapter._bootstrap_bot(bot_info)

    assert len(adapter.request_calls) == 1


@pytest.mark.asyncio
async def test_bootstrap_empty_token_skips_whoami(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty access_token should skip whoami and go straight to recovery."""
    from nonebot.adapters.matrix.oauth import OAuth2TokenResponse

    async def mock_oauth_login(bot_info: BotInfo) -> OAuth2TokenResponse:
        bot_info.oauth_token_endpoint = "https://auth.example.org/token"
        return OAuth2TokenResponse(
            access_token="oauth-token",
            refresh_token="oauth-refresh",
            expires_in=300,
        )

    store_path = tmp_path / "tokens.json"
    adapter = DummyAdapter(
        token_store_path=str(store_path),
        responses=[
            (200, b'{"user_id":"@bot:example.org","device_id":"DEV-EMPTY"}'),
        ],
    )
    monkeypatch.setattr(adapter, "_run_oauth2_login", mock_oauth_login)

    bot_info = BotInfo(
        homeserver="https://matrix.example.org",
        access_token="",  # empty!
        oauth_enabled=True,
        oauth_client_id="client-123",
        user_id="@bot:example.org",
    )

    self_info = await adapter._bootstrap_bot(bot_info)

    assert self_info.user_id == "@bot:example.org"
    assert bot_info.access_token == "oauth-token"
    # Only one request (whoami after OAuth2 login), no illegal whoami with empty token
    assert len(adapter.request_calls) == 1
