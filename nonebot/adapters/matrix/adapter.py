from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from functools import lru_cache
import inspect
from typing import Any
from typing_extensions import override

from nonebot.adapters import Adapter as BaseAdapter, Bot as BaseBot

from nonebot.drivers import URL, Driver, ForwardDriver
from nonebot.plugin import get_plugin_config
from nonebot.utils import escape_tag

from .api.handle import HandleMixin
from .api.model import RawMatrixEvent, SyncResponse, WhoamiResponse
from .api.types import UserId
from .bot import Bot
from .config import BotInfo, Config
from .event import InviteEvent, LeaveEvent, event_from_raw
from .exception import ApiNotAvailable, RateLimitException
from .utils import log

CLIENT_API_PREFIX = "/_matrix/client/v3"
MEDIA_API_PREFIX = "/_matrix/media/v3"


@lru_cache(maxsize=256)
def _get_handler_params(handler: Callable[..., Any]) -> Mapping[str, inspect.Parameter]:
    return inspect.signature(handler).parameters


class Adapter(BaseAdapter, HandleMixin):
    @override
    def __init__(self, driver: Driver, **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)
        self.matrix_config: Config = get_plugin_config(Config)
        self.tasks: set[asyncio.Task[None]] = set()
        self.setup()

    @classmethod
    @override
    def get_name(cls) -> str:
        return "Matrix"

    def setup(self) -> None:
        if not isinstance(self.driver, ForwardDriver):
            msg = (
                f"Current driver {self.config.driver} doesn't support forward "
                "connections! Matrix Adapter needs a ForwardDriver to work."
            )
            raise RuntimeError(msg)  # noqa: TRY004
        self.on_ready(self.startup)
        self.driver.on_shutdown(self.shutdown)

    async def startup(self) -> None:
        log("INFO", "Matrix Adapter is starting up...")
        for bot_info in self.matrix_config.matrix_bots:
            self.tasks.add(asyncio.create_task(self.run_bot(bot_info)))

    async def shutdown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()

    @staticmethod
    def get_authorization(bot_info: BotInfo) -> str:
        return f"Bearer {bot_info.access_token}"

    @staticmethod
    def _homeserver(bot_info: BotInfo) -> URL:
        return URL(bot_info.homeserver.rstrip("/"))

    def client_url(self, bot_info: BotInfo, path: str) -> URL:
        # Matrix identifiers are already percent-encoded by endpoint builders;
        # encoded=True prevents yarl from normalizing reserved characters back.
        return URL(
            f"{self._homeserver(bot_info)}{CLIENT_API_PREFIX}{path}", encoded=True
        )

    def media_url(self, bot_info: BotInfo, path: str) -> URL:
        return URL(
            f"{self._homeserver(bot_info)}{MEDIA_API_PREFIX}{path}", encoded=True
        )

    async def run_bot(self, bot_info: BotInfo) -> None:
        while True:
            bot: Bot | None = None
            try:
                self_info = await self._bootstrap_bot(bot_info)
                bot = Bot(self, str(self_info.user_id), bot_info, self_info)
                self.bot_connect(bot)
                log("INFO", f"Matrix bot {escape_tag(bot.self_id)} connected")
                await self._sync_loop(bot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("ERROR", "Matrix bot loop failed; retrying...", e)
                await asyncio.sleep(self.matrix_config.matrix_retry_interval)
            finally:
                if bot and bot.self_id in self.bots:
                    self.bot_disconnect(bot)

    async def _bootstrap_bot(self, bot_info: BotInfo) -> WhoamiResponse:
        # The adapter runs with configured tokens; whoami validates the token and
        # normalizes the canonical Matrix user/device identity before connecting.
        temp_bot = Bot(
            self,
            bot_info.user_id or "@unknown:invalid",
            bot_info,
            WhoamiResponse(
                user_id=UserId(bot_info.user_id or "@unknown:invalid"),
                device_id=bot_info.device_id,
            ),
        )
        self_info = await self._api_whoami(temp_bot)
        if bot_info.user_id is not None and bot_info.user_id != str(self_info.user_id):
            msg = (
                f"Configured Matrix user_id {bot_info.user_id!r} does not match "
                f"token owner {self_info.user_id!r}"
            )
            raise RuntimeError(msg)
        return self_info

    async def _sync_loop(self, bot: Bot) -> None:
        while True:
            try:
                sync = await self._api_sync(
                    bot,
                    since=bot.next_batch,
                    timeout=self.matrix_config.matrix_sync_timeout,
                    filter=bot.bot_info.sync_filter,
                    set_presence=bot.bot_info.set_presence,
                )
                bot.next_batch = sync.next_batch
                await self._handle_sync(bot, sync)
            except RateLimitException as e:  # noqa: PERF203
                delay = (e.retry_after_ms or 0) / 1000
                await asyncio.sleep(delay or self.matrix_config.matrix_retry_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log("ERROR", f"Error while syncing Matrix bot {bot.self_id}", e)
                await asyncio.sleep(self.matrix_config.matrix_retry_interval)

    async def _handle_sync(self, bot: Bot, sync: SyncResponse) -> None:
        self._update_direct_rooms(bot, sync.account_data.events)
        for room_id, room in sync.rooms.join.items():
            self._update_direct_rooms(bot, room.account_data.events)
            for raw in [*room.state.events, *room.timeline.events]:
                await self._dispatch_room_event(bot, raw, room_id=str(room_id))
            for raw in room.ephemeral.events:
                await self._dispatch_room_event(bot, raw, room_id=str(room_id))
        for room_id, room in sync.rooms.invite.items():
            event = InviteEvent(type="m.room.invite", room_id=room_id, content={})
            await bot.handle_event(event)
            for raw in room.invite_state.events:
                await self._dispatch_room_event(bot, raw, room_id=str(room_id))
        for room_id, room in sync.rooms.leave.items():
            event = LeaveEvent(type="m.room.leave", room_id=room_id, content={})
            await bot.handle_event(event)
            for raw in [*room.state.events, *room.timeline.events]:
                await self._dispatch_room_event(bot, raw, room_id=str(room_id))

    def _update_direct_rooms(self, bot: Bot, events: list[RawMatrixEvent]) -> None:
        for event in events:
            if event.type != "m.direct" or not isinstance(event.content, dict):
                continue
            # m.direct is per-account metadata; use it as the safest room-level
            # signal for whether unmentioned messages should be addressed to bot.
            for rooms in event.content.values():
                if isinstance(rooms, list):
                    bot.direct_rooms.update(str(room_id) for room_id in rooms)

    async def _dispatch_room_event(
        self,
        bot: Bot,
        raw: RawMatrixEvent,
        *,
        room_id: str,
    ) -> None:
        if (
            raw.sender == bot.user_id
            and not self.matrix_config.matrix_handle_self_message
        ):
            return
        to_me = self._is_to_me(bot, raw, room_id=room_id)
        event = event_from_raw(raw, room_id=room_id, to_me=to_me)
        await bot.handle_event(event)

    def _is_to_me(self, bot: Bot, raw: RawMatrixEvent, *, room_id: str) -> bool:
        if room_id in bot.direct_rooms:
            return True
        mentions = raw.content.get("m.mentions")
        if isinstance(mentions, dict):
            user_ids = mentions.get("user_ids")
            if isinstance(user_ids, list) and bot.self_id in user_ids:
                return True
        body = raw.content.get("body")
        return isinstance(body, str) and bot.self_id in body

    @override
    async def _call_api(self, bot: BaseBot, api: str, **data: Any) -> Any:
        if not isinstance(bot, Bot):
            msg = "Matrix adapter can only call API with Matrix Bot"
            raise TypeError(msg)
        handler = getattr(self, f"_api_{api}", None)
        if handler is None:
            raise ApiNotAvailable
        params = _get_handler_params(handler)
        kwargs = {key: value for key, value in data.items() if key in params}
        return await handler(bot, **kwargs)
