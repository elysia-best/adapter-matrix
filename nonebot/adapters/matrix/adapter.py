from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import contextlib
from functools import lru_cache
from http import HTTPStatus
import inspect
import json
from pathlib import Path
import sys
from time import time
from typing import Any
from typing_extensions import override
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from nonebot.adapters import Adapter as BaseAdapter, Bot as BaseBot

from nonebot.drivers import URL, Driver, ForwardDriver, Request
from nonebot.plugin import get_plugin_config
from nonebot.utils import escape_tag

from .api.handle import HandleMixin
from .api.model import (
    InvitedRoomSync,
    JoinedRoomSync,
    LeftRoomSync,
    LoginResponse,
    RawMatrixEvent,
    SyncResponse,
    WhoamiResponse,
)
from .api.types import RoomId, UserId
from .bot import Bot
from .config import BotInfo, Config
from .crypto import CryptoEngine
from .event import InviteEvent, LeaveEvent, event_from_raw
from .exception import (
    ApiNotAvailable,
    NetworkError,
    RateLimitException,
    UnauthorizedException,
)
from .oauth import (
    AuthorizationRequest,
    ClientRegistrationRequest,
    LocalCallbackServer,
    OAuth2DiscoveryError,
    OAuth2Error,
    OAuth2TokenResponse,
    TokenExchangeRequest,
    build_authorization_url,
    create_pkce_pair,
    discover_oauth_metadata,
    exchange_authorization_code,
    generate_device_id,
    generate_token,
    refresh_oauth_token,
    register_oauth_client,
)
from .utils import log

CLIENT_API_PREFIX = "/_matrix/client/v3"
MEDIA_API_PREFIX = "/_matrix/media/v3"
UNKNOWN_USER_ID = "@unknown:invalid"


@lru_cache(maxsize=256)
def _get_handler_params(handler: Callable[..., Any]) -> Mapping[str, inspect.Parameter]:
    return inspect.signature(handler).parameters


def _parse_oauth_code(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    if code:
        return code
    params = parse_qs(parsed.fragment)
    return params.get("code", [None])[0]


class Adapter(BaseAdapter, HandleMixin):
    @override
    def __init__(self, driver: Driver, **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)
        self.matrix_config: Config = get_plugin_config(Config)
        self.tasks: set[asyncio.Task[None]] = set()
        self._token_lifetimes_ms: dict[str, int] = {}
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

    def _token_store_path(self) -> Path | None:
        path = self.matrix_config.matrix_token_store_path
        return Path(path) if path else None

    def _bot_store_key(
        self, bot_info: BotInfo, *, user_id: str | None = None
    ) -> str | None:
        resolved_user_id = user_id or bot_info.user_id
        if resolved_user_id is None:
            return None
        return json.dumps(
            {
                "homeserver": bot_info.homeserver.rstrip("/"),
                "user_id": resolved_user_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _read_token_store(self) -> dict[str, Any]:
        path = self._token_store_path()
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log(
                "ERROR",
                f"Failed to read Matrix token store {path}: {type(e).__name__}: {e}",
            )
            return {}
        if isinstance(raw, dict):
            return raw
        log("ERROR", f"Matrix token store {path} is not a JSON object")
        return {}

    def _load_persisted_tokens(self, bot_info: BotInfo) -> None:  # noqa: C901
        key = self._bot_store_key(bot_info)
        if key is None:
            return
        payload = self._read_token_store().get(key)
        if not isinstance(payload, dict):
            return
        access_token = payload.get("access_token")
        if isinstance(access_token, str) and access_token:
            bot_info.access_token = access_token
        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            bot_info.refresh_token = refresh_token
        elif refresh_token is None:
            bot_info.refresh_token = None
        expires_at = payload.get("access_token_expires_at_ms")
        bot_info.access_token_expires_at_ms = (
            expires_at if isinstance(expires_at, int) else None
        )
        device_id = payload.get("device_id")
        if isinstance(device_id, str) and device_id:
            bot_info.device_id = device_id
        session_type = payload.get("session_type")
        if isinstance(session_type, str):
            bot_info.session_type = session_type
        elif session_type is None:
            bot_info.session_type = None
        oauth_token_endpoint = payload.get("oauth_token_endpoint")
        if isinstance(oauth_token_endpoint, str):
            bot_info.oauth_token_endpoint = oauth_token_endpoint
        oauth_client_id = payload.get("oauth_client_id")
        if isinstance(oauth_client_id, str) and oauth_client_id:
            bot_info.oauth_client_id = oauth_client_id

    def _save_persisted_tokens(
        self, bot_info: BotInfo, self_info: WhoamiResponse
    ) -> None:
        path = self._token_store_path()
        if path is None:
            return
        key = self._bot_store_key(bot_info, user_id=str(self_info.user_id))
        if key is None:
            return
        store = self._read_token_store()
        store[key] = {
            "access_token": bot_info.access_token,
            "refresh_token": bot_info.refresh_token,
            "access_token_expires_at_ms": bot_info.access_token_expires_at_ms,
            "device_id": str(self_info.device_id) if self_info.device_id else None,
            "session_type": bot_info.session_type,
            "oauth_token_endpoint": bot_info.oauth_token_endpoint,
            "oauth_client_id": bot_info.oauth_client_id,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        payload = json.dumps(store, ensure_ascii=False, sort_keys=True, indent=2)
        try:
            temp_path.write_text(payload + "\n", encoding="utf-8")
            temp_path.replace(path)
        except OSError as e:
            log(
                "ERROR",
                f"Failed to write Matrix token store {path}: {type(e).__name__}: {e}",
            )
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)

    def _apply_token_update(  # noqa: PLR0913
        self,
        bot_info: BotInfo,
        *,
        access_token: str | None,
        refresh_token: str | None,
        expires_in_ms: int | None,
        device_id: str | None = None,
        session_type: str | None = None,
    ) -> None:
        if access_token:
            bot_info.access_token = access_token
        if refresh_token:
            bot_info.refresh_token = refresh_token
        key = self._bot_store_key(bot_info)
        if expires_in_ms is None:
            bot_info.access_token_expires_at_ms = None
            if key is not None:
                self._token_lifetimes_ms.pop(key, None)
        else:
            bot_info.access_token_expires_at_ms = int(time() * 1000) + expires_in_ms
            if key is not None:
                self._token_lifetimes_ms[key] = expires_in_ms
        if device_id:
            bot_info.device_id = device_id
        if session_type is not None:
            bot_info.session_type = session_type

    def _should_refresh_token(self, bot: Bot) -> bool:
        expires_at = bot.bot_info.access_token_expires_at_ms
        if bot.bot_info.refresh_token is None or expires_at is None:
            return False
        key = self._bot_store_key(bot.bot_info)
        if key is not None:
            lifetime_ms = self._token_lifetimes_ms.get(key)
            if (
                lifetime_ms is not None
                and lifetime_ms <= bot.bot_info.refresh_before_expiry_ms
            ):
                return False
        return int(time() * 1000) > expires_at - bot.bot_info.refresh_before_expiry_ms

    async def _refresh_legacy_access_token(self, bot: Bot) -> WhoamiResponse:
        """Refresh tokens via Matrix /refresh endpoint."""
        refresh_token = bot.bot_info.refresh_token
        if refresh_token is None:
            msg = f"Matrix bot {bot.self_id} has no refresh token"
            raise RuntimeError(msg)
        response = await self._api_refresh_token(bot, refresh_token=refresh_token)
        self._apply_token_update(
            bot.bot_info,
            access_token=response.access_token,
            refresh_token=response.refresh_token,
            expires_in_ms=response.expires_in_ms,
        )
        self_info = await self._api_whoami(bot)
        bot.update_self_info(self_info)
        self._save_persisted_tokens(bot.bot_info, self_info)
        return self_info

    async def _refresh_oauth_access_token(self, bot: Bot) -> WhoamiResponse:
        """Refresh tokens via OAuth2 token endpoint."""
        bot_info = bot.bot_info
        token_endpoint = bot_info.oauth_token_endpoint
        client_id = bot_info.oauth_client_id
        refresh_token = bot_info.refresh_token
        if not token_endpoint or not client_id or not refresh_token:
            msg = f"Matrix bot {bot.self_id} missing OAuth2 refresh parameters"
            raise RuntimeError(msg)

        async def _post_form(
            url: str, data: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            body = urlencode(data).encode("ascii")
            resp = await self.request(
                Request(
                    "POST",
                    URL(url),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content=body,
                )
            )
            parsed = json.loads(resp.content) if resp.content else {}
            return resp.status_code, parsed

        oauth_response = await refresh_oauth_token(
            token_endpoint=token_endpoint,
            client_id=client_id,
            refresh_token=refresh_token,
            http_post_form=_post_form,
        )
        self._apply_token_update(
            bot_info,
            access_token=oauth_response.access_token,
            refresh_token=oauth_response.refresh_token,
            expires_in_ms=(oauth_response.expires_in * 1000)
            if oauth_response.expires_in is not None
            else None,
        )
        self_info = await self._api_whoami(bot)
        bot.update_self_info(self_info)
        self._save_persisted_tokens(bot_info, self_info)
        return self_info

    async def _refresh_access_token(self, bot: Bot) -> WhoamiResponse:
        """Dispatch token refresh based on session_type."""
        if bot.bot_info.session_type == "oauth2":
            return await self._refresh_oauth_access_token(bot)
        return await self._refresh_legacy_access_token(bot)

    async def _login_for_tokens(self, bot_info: BotInfo) -> LoginResponse:
        """Log in with password to obtain the first token pair."""
        if not bot_info.login_password:
            msg = "login_password is required for traditional Matrix login"
            raise RuntimeError(msg)
        temp_bot = Bot(
            self,
            bot_info.user_id or UNKNOWN_USER_ID,
            bot_info,
            WhoamiResponse(
                user_id=UserId(bot_info.user_id or UNKNOWN_USER_ID),
                device_id=bot_info.device_id,
            ),
        )
        return await self._api_login(
            temp_bot,
            password=bot_info.login_password,
            user=bot_info.login_user,
            device_id=bot_info.device_id,
            initial_device_display_name=bot_info.login_initial_device_display_name,
            refresh_token=True,
        )

    async def _run_oauth2_login(self, bot_info: BotInfo) -> OAuth2TokenResponse:
        """Run the OAuth2 authorization code flow to obtain the first token pair."""
        _get_json, _post_json, _post_form = self._make_oauth_http_helpers()

        metadata = await discover_oauth_metadata(
            bot_info.homeserver,
            _get_json,
            server_url=bot_info.oauth_server_url,
            metadata_url=bot_info.oauth_metadata_url,
        )
        bot_info.oauth_token_endpoint = metadata.token_endpoint

        (
            redirect_uri,
            callback_server,
            application_type,
        ) = await self._setup_oauth_redirect_uri(bot_info)

        if not bot_info.oauth_client_id:
            if not metadata.registration_endpoint:
                msg = "oauth_client_id is required because registration_endpoint is unavailable"
                raise OAuth2Error(msg)
            log("INFO", "Auto-registering OAuth2 client")
            client_name = (
                bot_info.login_initial_device_display_name
                or bot_info.login_user
                or "Matrix Bot"
            )
            registration = await register_oauth_client(
                ClientRegistrationRequest(
                    registration_endpoint=metadata.registration_endpoint,
                    client_name=client_name,
                    redirect_uris=[redirect_uri],
                    client_uri=bot_info.oauth_client_uri or bot_info.homeserver,
                    application_type=application_type,
                ),
                http_post_json=_post_json,
            )
            bot_info.oauth_client_id = registration.client_id
            log("INFO", f"Registered OAuth2 client: {registration.client_id}")

        # Hydrogen-style flow: generate device id first, then embed it in MSC2967 scope.
        device_id = (
            bot_info.oauth_device_id or bot_info.device_id or generate_device_id()
        )
        bot_info.device_id = device_id

        code_verifier, code_challenge = create_pkce_pair()
        state = generate_token()

        device_scope = f"urn:matrix:org.matrix.msc2967.client:device:{device_id}"
        scope = bot_info.oauth_scope or "urn:matrix:org.matrix.msc2967.client:api:*"
        if device_scope not in scope.split():
            scope = f"{scope} {device_scope}"
        nonce = generate_token() if "openid" in scope.split() else None

        # Equivalent to Hydrogen's authorizationEndpoint(): fragment mode + PKCE S256.
        auth_url = build_authorization_url(
            AuthorizationRequest(
                authorization_endpoint=metadata.authorization_endpoint,
                client_id=bot_info.oauth_client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                state=state,
                nonce=nonce,
                scope=scope,
            ),
        )

        log("INFO", f"OAuth2 authorization URL: {auth_url}")
        if bot_info.oauth_open_browser:
            webbrowser.open(auth_url)

        code = await self._collect_oauth_code(callback_server)
        if not code:
            msg = "No authorization code in OAuth2 callback"
            raise OAuth2Error(msg)

        return await exchange_authorization_code(
            TokenExchangeRequest(
                token_endpoint=metadata.token_endpoint,
                client_id=bot_info.oauth_client_id,
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            ),
            http_post_form=_post_form,
        )

    def _make_oauth_http_helpers(
        self,
    ) -> tuple[
        Callable[[str], Awaitable[dict[str, Any]]],
        Callable[[str, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]],
        Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]],
    ]:
        """Return the HTTP helper functions used during OAuth2 flow."""

        async def _get_json(url: str) -> dict[str, Any]:
            resp = await self.request(Request("GET", URL(url)))
            if (
                resp.status_code < HTTPStatus.OK
                or resp.status_code >= HTTPStatus.MULTIPLE_CHOICES
            ):
                msg = f"OAuth2 discovery HTTP {resp.status_code} for {url}"
                raise OAuth2DiscoveryError(msg)
            if not resp.content:
                msg = f"OAuth2 discovery returned empty body for {url}"
                raise OAuth2DiscoveryError(msg)

            try:
                return json.loads(resp.content)
            except json.JSONDecodeError as e:
                msg = f"OAuth2 discovery returned non-JSON response for {url}"
                raise OAuth2DiscoveryError(msg) from e

        async def _post_json(
            url: str, data: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            body = json.dumps(data).encode("utf-8")
            resp = await self.request(
                Request(
                    "POST",
                    URL(url),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    content=body,
                )
            )
            parsed_body = json.loads(resp.content) if resp.content else {}
            return resp.status_code, parsed_body

        async def _post_form(
            url: str, data: dict[str, str]
        ) -> tuple[int, dict[str, Any]]:
            body = urlencode(data).encode("ascii")
            resp = await self.request(
                Request(
                    "POST",
                    URL(url),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content=body,
                )
            )
            parsed_body = json.loads(resp.content) if resp.content else {}
            return resp.status_code, parsed_body

        return _get_json, _post_json, _post_form

    async def _setup_oauth_redirect_uri(
        self, bot_info: BotInfo
    ) -> tuple[str, LocalCallbackServer | None, str]:
        """Set up the OAuth2 redirect URI and callback server."""
        redirect_uri = bot_info.oauth_redirect_uri
        callback_server: LocalCallbackServer | None = None
        application_type = "web"
        if redirect_uri is None:
            callback_server = LocalCallbackServer(
                timeout=bot_info.oauth_callback_timeout,
                host="127.0.0.1",
                port=0,
                callback_path="/callback",
            )
            host, port = await callback_server.start()
            redirect_uri = f"http://{host}:{port}/callback"
            application_type = "native"
        else:
            parsed = urlparse(redirect_uri)
            if parsed.hostname in ("127.0.0.1", "localhost"):
                if parsed.port is None:
                    msg = (
                        "Loopback oauth_redirect_uri must include an explicit port. "
                        "If you want an automatically chosen port, omit oauth_redirect_uri."
                    )
                    raise OAuth2Error(msg)
                callback_server = LocalCallbackServer(
                    timeout=bot_info.oauth_callback_timeout,
                    host=parsed.hostname,
                    port=parsed.port,
                    callback_path=parsed.path or "/",
                )
                await callback_server.start()
                application_type = "native"
        return redirect_uri, callback_server, application_type

    async def _collect_oauth_code(
        self, callback_server: LocalCallbackServer | None
    ) -> str | None:
        """Wait for and collect the OAuth2 authorization code."""
        if callback_server is not None:
            callback_url = await callback_server.wait_for_callback()
            await callback_server.shutdown()
            return _parse_oauth_code(callback_url)
        log("INFO", "Paste the authorization code or full redirect URL:")
        pasted = (
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
        ).strip()
        return _parse_oauth_code(pasted) or pasted

    def _should_retry_with_refresh(
        self, exception: UnauthorizedException, bot: Bot
    ) -> bool:
        if bot.bot_info.refresh_token is None:
            return False
        return exception.errcode in {None, "M_UNKNOWN_TOKEN"}

    async def _handle_sync_unauthorized(
        self, bot: Bot, e: UnauthorizedException
    ) -> bool:
        """Handle UnauthorizedException during sync. Returns True if caller should continue."""
        if self._should_retry_with_refresh(e, bot):
            try:
                self_info = await self._refresh_access_token(bot)
            except UnauthorizedException as refresh_error:
                log(
                    "ERROR",
                    f"Token refresh rejected (4xx) for {bot.self_id}: "
                    f"{type(refresh_error).__name__}: {refresh_error}",
                )
                if refresh_error.soft_logout:
                    self_info = await self._relogin_if_soft_logout(bot)
                    if self_info is not None:
                        return True
            except NetworkError:
                log(
                    "ERROR",
                    f"Token refresh network error for {bot.self_id}; "
                    "keeping existing refresh token for retry",
                )
            except Exception as refresh_error:
                log(
                    "ERROR",
                    f"Failed to refresh Matrix access token for {bot.self_id}: "
                    f"{type(refresh_error).__name__}: {refresh_error}",
                )
            else:
                log(
                    "INFO",
                    f"Refreshed Matrix access token after unauthorized sync "
                    f"for {escape_tag(str(self_info.user_id))}",
                )
                return True
        # Check for soft_logout on the original sync error too
        if e.soft_logout and bot.bot_info.refresh_token is None:
            self_info = await self._relogin_if_soft_logout(bot)
            if self_info is not None:
                return True
        return False

    async def _relogin_if_soft_logout(self, bot: Bot) -> WhoamiResponse | None:
        """Re-login if the bot was soft-logged-out and has login credentials."""
        if not bot.bot_info.login_password:
            return None
        if bot.bot_info.session_type != "legacy_login":
            return None
        log("INFO", f"Attempting re-login after soft logout for {bot.self_id}")
        login_resp = await self._login_for_tokens(bot.bot_info)
        self._apply_token_update(
            bot.bot_info,
            access_token=login_resp.access_token,
            refresh_token=login_resp.refresh_token,
            expires_in_ms=login_resp.expires_in_ms,
            device_id=login_resp.device_id,
            session_type="legacy_login",
        )
        self_info = await self._api_whoami(bot)
        bot.update_self_info(self_info)
        self._save_persisted_tokens(bot.bot_info, self_info)
        return self_info

    async def _bootstrap_with_refresh(self, temp_bot: Bot) -> WhoamiResponse:
        """Validate token with whoami; if unauthorized, try refresh, login or OAuth2."""
        bot_info = temp_bot.bot_info

        # Try whoami if we have a token to validate
        if bot_info.access_token:
            try:
                return await self._api_whoami(temp_bot)
            except UnauthorizedException:
                pass  # fall through to recovery logic below

        # Recovery: try refresh, login, or OAuth2
        # If we have a refresh token, try that first
        if bot_info.refresh_token:
            try:
                self_info = await self._refresh_access_token(temp_bot)
                log(
                    "INFO",
                    f"Refreshed Matrix access token during bootstrap for "
                    f"{escape_tag(str(self_info.user_id))}",
                )
            except Exception:
                log(
                    "WARNING",
                    "Token refresh failed during bootstrap, will try login",
                )
            else:
                return self_info

        # No working refresh token — try password login
        if bot_info.login_password:
            log("INFO", "Logging in with password to obtain tokens")
            login_resp = await self._login_for_tokens(bot_info)
            self._apply_token_update(
                bot_info,
                access_token=login_resp.access_token,
                refresh_token=login_resp.refresh_token,
                expires_in_ms=login_resp.expires_in_ms,
                device_id=login_resp.device_id,
                session_type="legacy_login",
            )
            self_info = await self._api_whoami(temp_bot)
            log(
                "INFO",
                f"Logged in as {escape_tag(str(self_info.user_id))}",
            )
            return self_info

        # Try OAuth2 login
        if bot_info.oauth_enabled:
            log("INFO", "Starting OAuth2 authorization code flow")
            try:
                token_resp = await self._run_oauth2_login(bot_info)
            except OAuth2Error as oauth_err:
                log(
                    "ERROR",
                    f"OAuth2 login failed: {type(oauth_err).__name__}: {oauth_err}",
                )
                raise
            self._apply_token_update(
                bot_info,
                access_token=token_resp.access_token,
                refresh_token=token_resp.refresh_token,
                expires_in_ms=(token_resp.expires_in * 1000)
                if token_resp.expires_in is not None
                else None,
                session_type="oauth2",
            )
            self_info = await self._api_whoami(temp_bot)
            log(
                "INFO",
                f"OAuth2 login completed as {escape_tag(str(self_info.user_id))}",
            )
            return self_info

        # Nothing worked — give a clear diagnostic
        if bot_info.refresh_token is None:
            log(
                "WARNING",
                "Matrix bot has only an access_token with no login credentials "
                "or OAuth2 configured. The adapter cannot automatically obtain "
                "a refresh token. To enable automatic token refresh, provide "
                "login_password/login_user for traditional Matrix login or "
                "enable OAuth2 with oauth_enabled=true and oauth_client_id.",
            )
        msg = (
            "Failed to bootstrap Matrix bot: "
            "access_token is invalid and no recovery method is available"
        )
        raise RuntimeError(msg)

    async def run_bot(self, bot_info: BotInfo) -> None:
        while True:
            bot: Bot | None = None
            try:
                self_info = await self._bootstrap_bot(bot_info)
                bot = Bot(self, str(self_info.user_id), bot_info, self_info)

                # 初始化 E2EE 加密引擎 (如果配置了存储路径)
                if (
                    bot_info.e2ee_store_path
                    or self.matrix_config.matrix_token_store_path
                ):
                    try:
                        bot.crypto = CryptoEngine(bot, self)
                        await bot.crypto.initialize()
                    except Exception as e:
                        log(
                            "WARNING",
                            f"E2EE 加密引擎初始化失败，将以明文模式运行: "
                            f"{type(e).__name__}: {e}",
                        )
                        bot.crypto = None

                self.bot_connect(bot)
                log("INFO", f"Matrix bot {escape_tag(bot.self_id)} connected")
                await self._sync_loop(bot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(
                    "ERROR",
                    f"Matrix bot loop failed; retrying...  {type(e).__name__}",
                )
                await asyncio.sleep(self.matrix_config.matrix_retry_interval)
            finally:
                if bot and bot.self_id in self.bots:
                    self.bot_disconnect(bot)

    async def _bootstrap_bot(self, bot_info: BotInfo) -> WhoamiResponse:
        # The adapter runs with configured tokens; whoami validates the token and
        # normalizes the canonical Matrix user/device identity before connecting.
        self._load_persisted_tokens(bot_info)
        temp_bot = Bot(
            self,
            bot_info.user_id or UNKNOWN_USER_ID,
            bot_info,
            WhoamiResponse(
                user_id=UserId(bot_info.user_id or UNKNOWN_USER_ID),
                device_id=bot_info.device_id,
            ),
        )
        self_info = await self._bootstrap_with_refresh(temp_bot)
        if bot_info.user_id is not None and bot_info.user_id != str(self_info.user_id):
            msg = (
                f"Configured Matrix user_id {bot_info.user_id!r} does not match "
                f"token owner {self_info.user_id!r}"
            )
            raise RuntimeError(msg)
        self._save_persisted_tokens(bot_info, self_info)
        return self_info

    async def _sync_loop(self, bot: Bot) -> None:
        while True:
            try:
                if self._should_refresh_token(bot):
                    self_info = await self._refresh_access_token(bot)
                    log(
                        "INFO",
                        f"Refreshed Matrix access token for {escape_tag(str(self_info.user_id))}",
                    )
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
            except UnauthorizedException as e:
                if await self._handle_sync_unauthorized(bot, e):
                    continue
                log(
                    "ERROR",
                    f"Error while syncing Matrix bot {bot.self_id}: {type(e).__name__}: {e}",
                )
                await asyncio.sleep(self.matrix_config.matrix_retry_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(
                    "ERROR",
                    f"Error while syncing Matrix bot {bot.self_id}: {type(e).__name__}: {e}",
                )
                await asyncio.sleep(self.matrix_config.matrix_retry_interval)

    async def _handle_sync(self, bot: Bot, sync: SyncResponse) -> None:
        await self._handle_e2ee_sync_data(bot, sync)
        self._update_direct_rooms(bot, sync.account_data.events)
        for room_id, room in sync.rooms.join.items():
            self._update_direct_rooms(bot, room.account_data.events)
            await self._handle_joined_room(bot, room_id, room)
        for room_id, room in sync.rooms.invite.items():
            await self._handle_invited_room(bot, room_id, room)
        for room_id, room in sync.rooms.leave.items():
            await self._handle_left_room(bot, room_id, room)

    async def _handle_e2ee_sync_data(self, bot: Bot, sync: SyncResponse) -> None:
        """Process device_lists and to_device events for E2EE."""
        if sync.device_lists is not None and bot.crypto is not None:
            await bot.crypto.handle_device_lists(
                changed=[str(u) for u in sync.device_lists.changed],
                left=[str(u) for u in sync.device_lists.left],
            )

        if sync.to_device is not None and bot.crypto is not None:
            await self._handle_to_device_events(bot, sync.to_device.events)

    async def _handle_to_device_events(
        self, bot: Bot, events: list[RawMatrixEvent]
    ) -> None:
        """Process to-device messages for E2EE key exchange."""
        for raw in events:
            if bot.crypto is None:
                break
            await self._try_handle_to_device(bot, raw)

    async def _try_handle_to_device(self, bot: Bot, raw: RawMatrixEvent) -> None:
        if bot.crypto is None:
            return
        try:
            await bot.crypto.handle_to_device_event(raw)
        except Exception as e:
            log(
                "WARNING",
                f"handle to-device event failed: {type(e).__name__}: {e}",
            )

    async def _handle_joined_room(
        self, bot: Bot, room_id: RoomId, room: JoinedRoomSync
    ) -> None:
        """Process events for a joined room, including E2EE state detection."""
        for raw in room.state.events:
            if raw.type == "m.room.encryption" and bot.crypto is not None:
                algorithm = raw.content.get("algorithm", "m.megolm.v1.aes-sha2")
                bot.crypto.mark_room_as_encrypted(str(room_id), algorithm)
            await self._dispatch_room_event(bot, raw, room_id=str(room_id))
        for raw in room.timeline.events:
            await self._dispatch_room_event(bot, raw, room_id=str(room_id))
        for raw in room.ephemeral.events:
            await self._dispatch_room_event(bot, raw, room_id=str(room_id))

    async def _handle_invited_room(
        self, bot: Bot, room_id: RoomId, room: InvitedRoomSync
    ) -> None:
        """Process events for an invited room and optionally auto-join."""
        event = InviteEvent(type="m.room.invite", room_id=room_id, content={})
        await bot.handle_event(event)
        for raw in room.invite_state.events:
            await self._dispatch_room_event(bot, raw, room_id=str(room_id))
        inviter = self._get_inviter(bot, room)
        if inviter is not None and self._should_accept_invite(bot, inviter):
            log("INFO", f"Auto-accepting invite to room {room_id} from {inviter}")
            try:
                await self._api_join_room(bot, room_id=room_id)
            except Exception as e:
                log(
                    "ERROR",
                    f"Failed to auto-join room {room_id}: {type(e).__name__}: {e}",
                )

    async def _handle_left_room(
        self, bot: Bot, room_id: RoomId, room: LeftRoomSync
    ) -> None:
        """Process events for a left room."""
        event = LeaveEvent(type="m.room.leave", room_id=room_id, content={})
        await bot.handle_event(event)
        for raw in [*room.state.events, *room.timeline.events]:
            await self._dispatch_room_event(bot, raw, room_id=str(room_id))

    def _get_inviter(self, bot: Bot, room: InvitedRoomSync) -> str | None:
        """Extract the inviter's user ID from an invited room's state events."""
        for raw in room.invite_state.events:
            if (
                raw.type == "m.room.member"
                and raw.state_key == bot.user_id
                and isinstance(raw.content, dict)
                and raw.content.get("membership") == "invite"
            ):
                sender = raw.sender
                return str(sender) if sender is not None else None
        return None

    def _should_accept_invite(self, bot: Bot, inviter: str) -> bool:
        """Check whether an invite from the given inviter should be auto-accepted."""
        bot_info = bot.bot_info
        if not bot_info.auto_accept_invites:
            return False
        blacklist = bot_info.auto_accept_blacklist
        if blacklist and inviter in blacklist:
            return False
        whitelist = bot_info.auto_accept_whitelist
        return not (whitelist is not None and inviter not in whitelist)

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
        if self._is_old_event(bot, raw):
            return

        # 解密 m.room.encrypted 事件
        if raw.type == "m.room.encrypted" and bot.crypto is not None:
            try:
                decrypted = await bot.crypto.decrypt_room_event(raw, room_id=room_id)
                if decrypted is not None:
                    raw = decrypted
                else:
                    # Unable to decrypt (missing session key), skip dispatch
                    return
            except Exception as e:
                log(
                    "WARNING",
                    f"解密房间事件失败 ({room_id}): {type(e).__name__}: {e}",
                )
                return

        if (
            raw.sender == bot.user_id
            and not self.matrix_config.matrix_handle_self_message
        ):
            return
        to_me = self._is_to_me(bot, raw, room_id=room_id)
        event = event_from_raw(raw, room_id=room_id, to_me=to_me)
        await bot.handle_event(event)

    def _is_old_event(self, bot: Bot, raw: RawMatrixEvent) -> bool:
        return (
            not self.matrix_config.matrix_handle_old_events
            and raw.origin_server_ts is not None
            and raw.origin_server_ts < bot.startup_time_ms
        )

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
