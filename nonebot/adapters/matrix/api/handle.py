from __future__ import annotations

from http import HTTPStatus
import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from nonebot.compat import type_validate_python
from nonebot.drivers import Request, Response
from nonebot.utils import escape_tag
from yarl import URL

from .model import (
    EventIdResponse,
    JoinRoomResponse,
    LoginFlowsResponse,
    LoginIdentifier,
    LoginResponse,
    MediaConfigResponse,
    MembersChunkResponse,
    MessagesResponse,
    PasswordLoginRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RelationsResponse,
    SyncResponse,
    UploadResponse,
    WhoamiResponse,
)
from .types import EventId, EventType, ReceiptType, RoomIdentifier, TxnId, UserId
from .utils import filter_unset_query
from ..config import BotInfo, Config
from ..exception import (
    ActionFailed,
    RateLimitException,
    UnauthorizedException,
)
from ..serialization import encode_json_text
from ..utils import log

if TYPE_CHECKING:
    from ..bot import Bot


class AdapterProtocol(Protocol):
    matrix_config: Config

    @staticmethod
    def get_authorization(bot_info: BotInfo) -> str: ...

    def client_url(self, bot_info: BotInfo, path: str) -> URL: ...

    def media_url(self, bot_info: BotInfo, path: str) -> URL: ...

    async def request(self, setup: Request) -> Response: ...


def quote_path(value: object) -> str:
    return quote(str(value), safe="")


async def _request(
    adapter: AdapterProtocol,
    bot_info: BotInfo,
    request: Request,
    *,
    parse_json: bool = True,
) -> Any:  # noqa: ANN401
    del bot_info
    request.timeout = adapter.matrix_config.matrix_api_timeout
    request.proxy = adapter.matrix_config.matrix_proxy
    data = await adapter.request(request)
    log(
        "TRACE",
        f"API code: {data.status_code} response: {escape_tag(str(data.content))}",
    )
    if HTTPStatus.OK <= data.status_code < HTTPStatus.MULTIPLE_CHOICES:
        if not data.content:
            raise ActionFailed(data)
        if not parse_json:
            return data.content
        return json.loads(data.content)
    if data.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise UnauthorizedException(data)
    if data.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        raise RateLimitException(data)
    raise ActionFailed(data)


def _headers(adapter: AdapterProtocol, bot_info: BotInfo) -> dict[str, str]:
    return {"Authorization": adapter.get_authorization(bot_info)}


def _json_request(
    *,
    method: str,
    url: URL,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Request:
    return Request(
        method,
        url,
        headers=headers,
        params=params,
        json=body,
    )


async def _send_event(
    adapter: AdapterProtocol,
    bot: Bot,
    data: dict[str, Any],
) -> EventIdResponse:
    path = (
        f"/rooms/{quote_path(data['room_id'])}/send/"
        f"{quote_path(data['event_type'])}/{quote_path(data['txn_id'])}"
    )
    payload = data["content"]
    response = await _request(
        adapter,
        bot.bot_info,
        _json_request(
            method="PUT",
            url=adapter.client_url(bot.bot_info, path),
            headers=_headers(adapter, bot.bot_info),
            body=payload,
        ),
    )
    return type_validate_python(EventIdResponse, response)


class HandleMixin:
    async def _api_get_login_flows(
        self: AdapterProtocol, bot: Bot
    ) -> LoginFlowsResponse:
        """Get supported Matrix login flows."""
        data = await _request(
            self,
            bot.bot_info,
            Request("GET", self.client_url(bot.bot_info, "/login")),
        )
        return type_validate_python(LoginFlowsResponse, data)

    async def _api_login(  # noqa: PLR0913
        self: AdapterProtocol,
        bot: Bot,
        *,
        password: str,
        user: str | None = None,
        identifier: dict[str, Any] | None = None,
        device_id: str | None = None,
        initial_device_display_name: str | None = None,
        refresh_token: bool | None = None,
    ) -> LoginResponse:
        """Log in with the Matrix password flow."""
        login_identifier = (
            type_validate_python(LoginIdentifier, identifier)
            if identifier is not None
            else None
        )
        payload = PasswordLoginRequest(
            user=user,
            identifier=login_identifier,
            password=password,
            device_id=device_id,
            initial_device_display_name=initial_device_display_name,
            refresh_token=refresh_token,
        )
        data = await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(bot.bot_info, "/login"),
                body=json.loads(
                    encode_json_text(
                        payload.model_dump(by_alias=True, exclude_none=True)
                    )
                ),
            ),
        )
        return type_validate_python(LoginResponse, data)

    async def _api_refresh_token(
        self: AdapterProtocol,
        bot: Bot,
        *,
        refresh_token: str,
    ) -> RefreshTokenResponse:
        """Refresh a Matrix access token."""
        payload = RefreshTokenRequest(refresh_token=refresh_token)
        data = await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(bot.bot_info, "/refresh"),
                body=json.loads(
                    encode_json_text(
                        payload.model_dump(by_alias=True, exclude_none=True)
                    )
                ),
            ),
        )
        return type_validate_python(RefreshTokenResponse, data)

    async def _api_logout(self: AdapterProtocol, bot: Bot) -> None:
        """Invalidate the current Matrix access token."""
        await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(bot.bot_info, "/logout"),
                headers=_headers(self, bot.bot_info),
                body={},
            ),
        )

    async def _api_logout_all(self: AdapterProtocol, bot: Bot) -> None:
        """Invalidate all access tokens for the current Matrix account."""
        await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(bot.bot_info, "/logout/all"),
                headers=_headers(self, bot.bot_info),
                body={},
            ),
        )

    async def _api_whoami(self: AdapterProtocol, bot: Bot) -> WhoamiResponse:
        """Return the Matrix user associated with the configured access token."""
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.client_url(bot.bot_info, "/account/whoami"),
                headers=_headers(self, bot.bot_info),
            ),
        )
        return type_validate_python(WhoamiResponse, data)

    async def _api_sync(  # noqa: PLR0913
        self: AdapterProtocol,
        bot: Bot,
        *,
        since: str | None = None,
        timeout: int | None = None,
        filter: str | dict[str, Any] | None = None,  # noqa: A002
        full_state: bool | None = None,
        set_presence: str | None = None,
        use_state_after: bool | None = None,
    ) -> SyncResponse:
        """Long-poll Matrix sync and return room/account/ephemeral events."""
        filter_value = encode_json_text(filter) if isinstance(filter, dict) else filter
        params = filter_unset_query(
            {
                "since": since,
                "timeout": timeout,
                "filter": filter_value,
                "full_state": str(full_state).lower()
                if full_state is not None
                else None,
                "set_presence": set_presence,
                "use_state_after": (
                    str(use_state_after).lower()
                    if use_state_after is not None
                    else None
                ),
            }
        )
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.client_url(bot.bot_info, "/sync"),
                headers=_headers(self, bot.bot_info),
                params=params,
                timeout=(timeout / 1000 + 5 if timeout else None),
            ),
        )
        return type_validate_python(SyncResponse, data)

    async def _api_send_event(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        event_type: EventType,
        txn_id: TxnId,
        content: dict[str, Any],
    ) -> EventIdResponse:
        """Send a Matrix event with a client-generated transaction id."""
        return await _send_event(
            self,
            bot,
            {
                "room_id": room_id,
                "event_type": event_type,
                "txn_id": txn_id,
                "content": content,
            },
        )

    async def _api_send_message(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        txn_id: TxnId,
        content: dict[str, Any],
    ) -> EventIdResponse:
        """Send an m.room.message event."""
        return await _send_event(
            self,
            bot,
            {
                "room_id": room_id,
                "event_type": "m.room.message",
                "txn_id": txn_id,
                "content": content,
            },
        )

    async def _api_upload_content(
        self: AdapterProtocol,
        bot: Bot,
        *,
        content: bytes,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadResponse:
        """Upload bytes to the Matrix media repository and return an mxc URI."""
        headers = _headers(self, bot.bot_info)
        if content_type:
            headers["Content-Type"] = content_type
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "POST",
                self.media_url(bot.bot_info, "/upload"),
                headers=headers,
                params=filter_unset_query({"filename": filename}),
                content=content,
            ),
        )
        return type_validate_python(UploadResponse, data)

    async def _api_get_media_config(
        self: AdapterProtocol,
        bot: Bot,
    ) -> MediaConfigResponse:
        """Get media repository limits for the homeserver."""
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.media_url(bot.bot_info, "/config"),
                headers=_headers(self, bot.bot_info),
            ),
        )
        return type_validate_python(MediaConfigResponse, data)

    async def _api_download_media(
        self: AdapterProtocol,
        bot: Bot,
        *,
        server_name: str,
        media_id: str,
        filename: str | None = None,
        allow_remote: bool | None = None,
    ) -> bytes | str:
        """Download a Matrix media item."""
        path = f"/download/{quote_path(server_name)}/{quote_path(media_id)}"
        if filename:
            path = f"{path}/{quote_path(filename)}"
        return await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.media_url(bot.bot_info, path),
                headers=_headers(self, bot.bot_info),
                params=filter_unset_query({"allow_remote": allow_remote}),
            ),
            parse_json=False,
        )

    async def _api_thumbnail_media(  # noqa: PLR0913
        self: AdapterProtocol,
        bot: Bot,
        *,
        server_name: str,
        media_id: str,
        width: int,
        height: int,
        method: str | None = None,
        allow_remote: bool | None = None,
    ) -> bytes | str:
        """Download a Matrix media thumbnail."""
        return await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.media_url(
                    bot.bot_info,
                    f"/thumbnail/{quote_path(server_name)}/{quote_path(media_id)}",
                ),
                headers=_headers(self, bot.bot_info),
                params=filter_unset_query(
                    {
                        "width": width,
                        "height": height,
                        "method": method,
                        "allow_remote": allow_remote,
                    }
                ),
            ),
            parse_json=False,
        )

    async def _api_redact_event(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        txn_id: TxnId,
        reason: str | None = None,
    ) -> EventIdResponse:
        """Redact a Matrix event in a room."""
        path = (
            f"/rooms/{quote_path(room_id)}/redact/"
            f"{quote_path(event_id)}/{quote_path(txn_id)}"
        )
        data = await _request(
            self,
            bot.bot_info,
            _json_request(
                method="PUT",
                url=self.client_url(bot.bot_info, path),
                headers=_headers(self, bot.bot_info),
                body=filter_unset_query({"reason": reason}),
            ),
        )
        return type_validate_python(EventIdResponse, data)

    async def _api_set_typing(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        user_id: UserId | str,
        typing: bool,
        timeout: int | None = None,
    ) -> None:
        """Set the bot typing state for a room."""
        await _request(
            self,
            bot.bot_info,
            _json_request(
                method="PUT",
                url=self.client_url(
                    bot.bot_info,
                    f"/rooms/{quote_path(room_id)}/typing/{quote_path(user_id)}",
                ),
                headers=_headers(self, bot.bot_info),
                body=filter_unset_query({"typing": typing, "timeout": timeout}),
            ),
        )

    async def _api_post_receipt(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        receipt_type: ReceiptType,
        event_id: EventId | str,
        thread_id: str | None = None,
    ) -> None:
        """Post a Matrix read receipt or read marker."""
        await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(
                    bot.bot_info,
                    f"/rooms/{quote_path(room_id)}/receipt/"
                    f"{quote_path(receipt_type)}/{quote_path(event_id)}",
                ),
                headers=_headers(self, bot.bot_info),
                body=filter_unset_query({"thread_id": thread_id}),
            ),
        )

    async def _api_get_room_members(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        at: str | None = None,
        membership: str | None = None,
        not_membership: str | None = None,
    ) -> MembersChunkResponse:
        """Get membership state events for a room."""
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.client_url(bot.bot_info, f"/rooms/{quote_path(room_id)}/members"),
                headers=_headers(self, bot.bot_info),
                params=filter_unset_query(
                    {
                        "at": at,
                        "membership": membership,
                        "not_membership": not_membership,
                    }
                ),
            ),
        )
        return type_validate_python(MembersChunkResponse, data)

    async def _api_get_room_messages(  # noqa: PLR0913
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        from_token: str,
        dir: str,  # noqa: A002
        to: str | None = None,
        limit: int | None = None,
        filter: str | dict[str, Any] | None = None,  # noqa: A002
    ) -> MessagesResponse:
        """Paginate Matrix room events."""
        filter_value = encode_json_text(filter) if isinstance(filter, dict) else filter
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.client_url(bot.bot_info, f"/rooms/{quote_path(room_id)}/messages"),
                headers=_headers(self, bot.bot_info),
                params=filter_unset_query(
                    {
                        "from": from_token,
                        "dir": dir,
                        "to": to,
                        "limit": limit,
                        "filter": filter_value,
                    }
                ),
            ),
        )
        return type_validate_python(MessagesResponse, data)

    async def _api_get_relations(  # noqa: PLR0913
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        rel_type: str | None = None,
        event_type: EventType | None = None,
        from_token: str | None = None,
        to: str | None = None,
        limit: int | None = None,
    ) -> RelationsResponse:
        """Get relation events for a Matrix event."""
        path = f"/rooms/{quote_path(room_id)}/relations/{quote_path(event_id)}"
        if rel_type:
            path = f"{path}/{quote_path(rel_type)}"
        if event_type:
            path = f"{path}/{quote_path(event_type)}"
        data = await _request(
            self,
            bot.bot_info,
            Request(
                "GET",
                self.client_url(bot.bot_info, path.replace("/rooms/", "/rooms/", 1)),
                headers=_headers(self, bot.bot_info),
                params=filter_unset_query(
                    {"from": from_token, "to": to, "limit": limit}
                ),
            ),
        )
        return type_validate_python(RelationsResponse, data)

    async def _api_create_reaction(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        event_id: EventId | str,
        key: str,
        txn_id: TxnId,
    ) -> EventIdResponse:
        """Send an m.reaction annotation relation."""
        return await _send_event(
            self,
            bot,
            {
                "room_id": room_id,
                "event_type": "m.reaction",
                "txn_id": txn_id,
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": str(event_id),
                        "key": key,
                    }
                },
            },
        )

    async def _api_join_room(
        self: AdapterProtocol,
        bot: Bot,
        *,
        room_id: RoomIdentifier,
        reason: str | None = None,
    ) -> JoinRoomResponse:
        """Join a Matrix room by ID."""
        data = await _request(
            self,
            bot.bot_info,
            _json_request(
                method="POST",
                url=self.client_url(bot.bot_info, f"/rooms/{quote_path(room_id)}/join"),
                headers=_headers(self, bot.bot_info),
                body=filter_unset_query({"reason": reason}),
            ),
        )
        return type_validate_python(JoinRoomResponse, data)
