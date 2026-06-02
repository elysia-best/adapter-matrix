from collections.abc import Sequence
from typing_extensions import override

from nonebot.adapters.matrix.adapter import Adapter
from nonebot.adapters.matrix.api.handle import HandleMixin
from nonebot.adapters.matrix.api.model import WhoamiResponse
from nonebot.adapters.matrix.bot import Bot
from nonebot.adapters.matrix.config import BotInfo, Config

from nonebot.drivers import Request, Response


class DummyAdapter(Adapter, HandleMixin):
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"{}",
        responses: Sequence[tuple[int, bytes]] | None = None,
        token_store_path: str | None = None,
    ) -> None:
        self.matrix_config = Config(matrix_token_store_path=token_store_path)
        self.status_code = status_code
        self.content = content
        self.responses = list(responses or [])
        self.request_calls: list[Request] = []
        self._token_lifetimes_ms: dict[str, int] = {}

    @override
    @staticmethod
    def get_authorization(bot_info: BotInfo) -> str:
        return f"Bearer {bot_info.access_token}"

    @override
    async def request(self, setup: Request) -> Response:
        self.request_calls.append(setup)
        if self.responses:
            status_code, content = self.responses.pop(0)
            return Response(status_code, content=content)
        return Response(self.status_code, content=self.content)


class DummyBot(Bot):
    def __init__(
        self,
        adapter: DummyAdapter | None = None,
        *,
        homeserver: str = "https://matrix.example.org",
        access_token: str = "test-token",  # noqa: S107
        refresh_token: str | None = None,
        access_token_expires_at_ms: int | None = None,
        refresh_before_expiry_ms: int = 60000,
        user_id: str = "@bot:example.org",
        device_id: str | None = None,
    ) -> None:
        if adapter is None:
            adapter = DummyAdapter()
        bot_info = BotInfo(
            homeserver=homeserver,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at_ms=access_token_expires_at_ms,
            refresh_before_expiry_ms=refresh_before_expiry_ms,
            user_id=user_id,
            device_id=device_id,
        )
        super().__init__(
            adapter=adapter,
            self_id=user_id,
            bot_info=bot_info,
            self_info=WhoamiResponse(user_id=user_id, device_id=device_id),
        )
