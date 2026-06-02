from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import secrets
import string
from typing import Any
from urllib.parse import urlencode

from requests_oauth2client import PkceUtils

HttpGetJson = Callable[[str], Awaitable[dict[str, Any]]]
HttpPostJson = Callable[[str, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]
HttpPostForm = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]]


@dataclass
class OAuth2Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    jwks_uri: str | None = None
    scopes_supported: list[str] = field(default_factory=list)
    device_authorization_endpoint: str | None = None


@dataclass
class OAuth2TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    token_type: str = "Bearer"
    scope: str | None = None


async def discover_oauth_metadata(
    homeserver: str,
    http_get_json: HttpGetJson,
    *,
    server_url: str | None = None,
    metadata_url: str | None = None,
) -> OAuth2Metadata:
    """Discover OAuth2 metadata per MSC2965.

    Priority:
    1. metadata_url — fetch this URL directly
    2. MSC2965 auth_metadata on the homeserver
    3. server_url — synthesize standard MAS endpoints from the configured base URL
    """
    if metadata_url:
        data = await http_get_json(metadata_url)
        metadata = _metadata_from_raw(data)
        validate_oauth_metadata(metadata, data)
        return metadata

    data = await _try_msc2965_discovery(homeserver, http_get_json)
    if data is not None:
        metadata = _metadata_from_raw(data)
        validate_oauth_metadata(metadata, data)
        return metadata

    if server_url:
        return _metadata_from_server_url(server_url)

    msg = "OAuth2 discovery failed: auth_metadata unavailable and no oauth_server_url configured"
    raise OAuth2DiscoveryError(msg)


async def _try_msc2965_discovery(
    homeserver: str, http_get_json: HttpGetJson
) -> dict[str, Any] | None:
    """Try MSC2965 auth_metadata endpoints directly on the homeserver."""
    from urllib.parse import urljoin

    urls = [
        urljoin(homeserver.rstrip("/") + "/", "_matrix/client/v1/auth_metadata"),
        urljoin(
            homeserver.rstrip("/") + "/",
            "_matrix/client/unstable/org.matrix.msc2965/auth_metadata",
        ),
    ]
    for url in urls:
        try:
            return await http_get_json(url)
        except OAuth2DiscoveryError:
            continue
    return None


def _metadata_from_raw(data: dict[str, Any]) -> OAuth2Metadata:
    return OAuth2Metadata(
        issuer=data.get("issuer", ""),
        authorization_endpoint=data.get("authorization_endpoint", ""),
        token_endpoint=data.get("token_endpoint", ""),
        registration_endpoint=data.get("registration_endpoint"),
        jwks_uri=data.get("jwks_uri"),
        scopes_supported=data.get("scopes_supported", []),
        device_authorization_endpoint=data.get("device_authorization_endpoint"),
    )


def _metadata_from_server_url(server_url: str) -> OAuth2Metadata:
    base = server_url.rstrip("/")
    return OAuth2Metadata(
        issuer=base,
        authorization_endpoint=f"{base}/authorize",
        token_endpoint=f"{base}/oauth2/token",
        registration_endpoint=f"{base}/oauth2/registration",
        jwks_uri=f"{base}/oauth2/keys.json",
        device_authorization_endpoint=f"{base}/oauth2/device",
    )


def generate_token(length: int = 32) -> str:
    """Generate a cryptographically random URL-safe token."""
    return secrets.token_urlsafe(length)


def generate_device_id(length: int = 12) -> str:
    """Generate an MSC2967-style Matrix device identifier."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def validate_oauth_metadata(metadata: OAuth2Metadata, raw: dict[str, Any]) -> None:
    """Validate the minimum capabilities required by the Matrix next-gen auth guide.

    Mirrors Hydrogen's validate() checks for code flow, fragment mode, and PKCE S256.
    registration_endpoint is handled later because this adapter also supports
    pre-registered static client IDs.
    """
    if not metadata.authorization_endpoint:
        raise OAuth2DiscoveryError("OAuth2 metadata missing authorization_endpoint")
    if not metadata.token_endpoint:
        raise OAuth2DiscoveryError("OAuth2 metadata missing token_endpoint")

    response_types = raw.get("response_types_supported")
    if not isinstance(response_types, list) or "code" not in response_types:
        raise OAuth2DiscoveryError("OAuth2 server does not support response_type=code")

    response_modes = raw.get("response_modes_supported")
    if not isinstance(response_modes, list) or "fragment" not in response_modes:
        raise OAuth2DiscoveryError("OAuth2 server does not support response_mode=fragment")

    grant_types = raw.get("grant_types_supported")
    if isinstance(grant_types, list) and "authorization_code" not in grant_types:
        raise OAuth2DiscoveryError(
            "OAuth2 server does not support grant_type=authorization_code"
        )

    challenge_methods = raw.get("code_challenge_methods_supported")
    if not isinstance(challenge_methods, list) or "S256" not in challenge_methods:
        raise OAuth2DiscoveryError("OAuth2 server does not support PKCE S256")


def create_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and S256 code_challenge."""
    verifier = PkceUtils.generate_code_verifier()
    challenge = PkceUtils.derive_challenge(verifier)
    return verifier, challenge


def build_authorization_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    *,
    nonce: str | None = None,
    code_challenge_method: str = "S256",
    scope: str = "openid urn:matrix:org.matrix.msc2967.client:api:*",
) -> str:
    """Build an OAuth2 authorization URL per MSC2964/MSC2967.

    Uses response_mode=fragment. Device ID is embedded in scope
    (urn:matrix:org.matrix.msc2967.client:device:{id}) per MSC2967.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "fragment",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
    }
    if nonce:
        params["nonce"] = nonce

    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


class LocalCallbackServer:
    """Async local HTTP server to capture OAuth2 authorization callback."""

    def __init__(
        self,
        timeout: float = 300.0,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        callback_path: str = "/callback",
    ) -> None:
        self._timeout = timeout
        self._host = host
        self._port = port
        self._callback_path = callback_path or "/callback"
        self._server: asyncio.AbstractServer | None = None
        self._callback_url: str | None = None
        self._event = asyncio.Event()

    async def start(self) -> tuple[str, int]:
        """Start listening on the configured loopback address and port."""
        self._server = await asyncio.start_server(
            self._handle_request, self._host, self._port
        )
        addr = self._server.sockets[0].getsockname()
        return addr[0], addr[1]

    async def wait_for_callback(self) -> str:
        """Wait for the OAuth2 redirect and return the full callback URL."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError as e:
            msg = f"Timed out waiting for OAuth2 callback after {self._timeout}s"
            raise OAuth2CallbackTimeoutError(msg) from e
        if self._callback_url is None:
            msg = "Callback server stopped without receiving a request"
            raise OAuth2CallbackError(msg)
        return self._callback_url

    async def shutdown(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=self._timeout
            )
            if not request_line:
                writer.close()
                return

            parts = request_line.decode("ascii", errors="replace").split()
            request_target = parts[1] if len(parts) >= 2 else "/"
            port = writer.get_extra_info("sockname")[1]
            callback_path = self._callback_path or "/"
            bridge_path = (
                f"{callback_path.rstrip('/')}/fragment"
                if callback_path != "/"
                else "/fragment"
            )

            if request_target.startswith(f"{bridge_path}?") and not self._event.is_set():
                self._callback_url = f"http://{self._host}:{port}{request_target}"
                self._event.set()
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"<html><body><h1>Authorization complete</h1>"
                    b"<p>You may close this window and return to the application.</p>"
                    b"</body></html>"
                )
            elif request_target == callback_path or request_target.startswith(
                f"{callback_path}?"
            ):
                # response_mode=fragment never reaches the server directly, so convert it
                # in the browser to a same-origin query request the local server can read.
                bridge_prefix = bridge_path.encode("utf-8")
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"<html><body><script>"
                    b"const h=window.location.hash.startsWith('#')?window.location.hash.slice(1):'';"
                    b"const q=window.location.search.startsWith('?')?window.location.search.slice(1):'';"
                    b"const p=new URLSearchParams(h||q);"
                    b"window.location.replace('" + bridge_prefix + b"?'+p.toString());"
                    b"</script><p>Completing authorization...</p></body></html>"
                )
            else:
                response = (
                    b"HTTP/1.1 404 Not Found\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"Not Found"
                )

            writer.write(response)
            await writer.drain()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()


async def exchange_authorization_code(
    token_endpoint: str,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    http_post_form: HttpPostForm,
    *,
    client_secret: str | None = None,
) -> OAuth2TokenResponse:
    """Exchange authorization code for tokens at the OAuth2 token endpoint."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret

    status, body = await http_post_form(token_endpoint, data)
    if status < 200 or status >= 300:
        msg = f"Authorization code exchange failed: HTTP {status}"
        raise OAuth2TokenError(msg, status=status, body=body)

    return OAuth2TokenResponse(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=body.get("expires_in"),
        token_type=body.get("token_type", "Bearer"),
        scope=body.get("scope"),
    )


async def refresh_oauth_token(
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    http_post_form: HttpPostForm,
    *,
    client_secret: str | None = None,
) -> OAuth2TokenResponse:
    """Refresh tokens using an OAuth2 refresh token grant."""
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret

    status, body = await http_post_form(token_endpoint, data)
    if status < 200 or status >= 300:
        msg = f"OAuth2 token refresh failed: HTTP {status}"
        raise OAuth2TokenError(msg, status=status, body=body)

    return OAuth2TokenResponse(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=body.get("expires_in"),
        token_type=body.get("token_type", "Bearer"),
        scope=body.get("scope"),
    )


@dataclass
class OAuth2ClientRegistration:
    client_id: str
    client_name: str | None = None
    redirect_uris: list[str] = field(default_factory=list)


async def register_oauth_client(
    registration_endpoint: str,
    client_name: str,
    redirect_uris: list[str],
    http_post_json: HttpPostJson,
    *,
    client_uri: str | None = None,
    application_type: str = "web",
) -> OAuth2ClientRegistration:
    """Dynamically register an OAuth2 client (RFC 7591)."""
    # Keep the payload minimal and aligned with the OIDC guide, while preserving
    # Hydrogen's required client_uri field for MAS implementations that enforce it.
    data = {
        "application_type": application_type,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if client_uri:
        data["client_uri"] = client_uri
    status, body = await http_post_json(registration_endpoint, data)
    if status < 200 or status >= 300:
        msg = f"OAuth2 client registration failed: HTTP {status} BODY {body}"
        raise OAuth2TokenError(msg, status=status, body=body)

    client_id = body.get("client_id")
    if not client_id:
        msg = "OAuth2 client registration response missing client_id"
        raise OAuth2Error(msg)

    return OAuth2ClientRegistration(
        client_id=client_id,
        client_name=body.get("client_name"),
        redirect_uris=body.get("redirect_uris", []),
    )


class OAuth2Error(Exception):
    """Base error for OAuth2 operations."""


class OAuth2DiscoveryError(OAuth2Error):
    """Failed to discover OAuth2 metadata."""


class OAuth2CallbackError(OAuth2Error):
    """Failed to receive OAuth2 callback."""


class OAuth2CallbackTimeoutError(OAuth2CallbackError):
    """Timed out waiting for OAuth2 callback."""


class OAuth2TokenError(OAuth2Error):
    """OAuth2 token endpoint returned an error."""

    def __init__(self, msg: str, *, status: int, body: dict[str, Any]) -> None:
        super().__init__(msg)
        self.status = status
        self.body = body
