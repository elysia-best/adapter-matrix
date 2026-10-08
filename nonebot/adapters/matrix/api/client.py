from __future__ import annotations

# The runtime facade forwards arbitrary endpoint keyword payloads; precise
# signatures are generated in client.pyi.
# ruff: noqa: ANN401
from typing import Any


class ApiClient:
    """Typed runtime facade for the adapter API.

    ``BaseBot.__getattr__`` still supports arbitrary adapter endpoints, while
    these methods make the public Matrix API explicit and usable by type checkers
    and introspection tools.
    """

    async def _matrix_call(self, name: str, **data: Any) -> Any:
        return await self.call_api(name, **data)  # type: ignore[attr-defined]

    async def get_login_flows(self) -> Any:
        return await self._matrix_call("get_login_flows")

    async def login(self, **data: Any) -> Any:
        return await self._matrix_call("login", **data)

    async def refresh_token(self, **data: Any) -> Any:
        return await self._matrix_call("refresh_token", **data)

    async def logout(self) -> None:
        await self._matrix_call("logout")

    async def logout_all(self) -> None:
        await self._matrix_call("logout_all")

    async def whoami(self) -> Any:
        return await self._matrix_call("whoami")

    async def sync(self, **data: Any) -> Any:
        return await self._matrix_call("sync", **data)

    async def send_event(self, **data: Any) -> Any:
        return await self._matrix_call("send_event", **data)

    async def send_message(self, **data: Any) -> Any:
        return await self._matrix_call("send_message", **data)

    async def upload_content(self, **data: Any) -> Any:
        return await self._matrix_call("upload_content", **data)

    async def get_media_config(self) -> Any:
        return await self._matrix_call("get_media_config")

    async def download_media(self, **data: Any) -> Any:
        return await self._matrix_call("download_media", **data)

    async def thumbnail_media(self, **data: Any) -> Any:
        return await self._matrix_call("thumbnail_media", **data)

    async def redact_event(self, **data: Any) -> Any:
        return await self._matrix_call("redact_event", **data)

    async def set_typing(self, **data: Any) -> None:
        await self._matrix_call("set_typing", **data)

    async def post_receipt(self, **data: Any) -> None:
        await self._matrix_call("post_receipt", **data)

    async def get_room_members(self, **data: Any) -> Any:
        return await self._matrix_call("get_room_members", **data)

    async def get_room_messages(self, **data: Any) -> Any:
        return await self._matrix_call("get_room_messages", **data)

    async def get_relations(self, **data: Any) -> Any:
        return await self._matrix_call("get_relations", **data)

    async def create_reaction(self, **data: Any) -> Any:
        return await self._matrix_call("create_reaction", **data)

    async def join_room(self, **data: Any) -> Any:
        return await self._matrix_call("join_room", **data)

    async def leave_room(self, **data: Any) -> Any:
        return await self._matrix_call("leave_room", **data)

    async def get_room_state(self, **data: Any) -> Any:
        return await self._matrix_call("get_room_state", **data)

    async def send_state_event(self, **data: Any) -> Any:
        return await self._matrix_call("send_state_event", **data)

    async def create_room(self, **data: Any) -> Any:
        return await self._matrix_call("create_room", **data)

    async def keys_upload(self, **data: Any) -> Any:
        return await self._matrix_call("keys_upload", **data)

    async def keys_query(self, **data: Any) -> Any:
        return await self._matrix_call("keys_query", **data)

    async def keys_claim(self, **data: Any) -> Any:
        return await self._matrix_call("keys_claim", **data)

    async def send_to_device(self, **data: Any) -> None:
        await self._matrix_call("send_to_device", **data)

    async def room_keys_version(self, **data: Any) -> Any:
        return await self._matrix_call("room_keys_version", **data)

    async def room_keys_keys(self, **data: Any) -> Any:
        return await self._matrix_call("room_keys_keys", **data)

    async def get_account_data(self, **data: Any) -> Any:
        return await self._matrix_call("get_account_data", **data)
