from typing_extensions import override

from nonebot.adapters.matrix.adapter import Adapter
from nonebot.adapters.matrix.api.handle import HandleMixin
from nonebot.adapters.matrix.api.model import WhoamiResponse
from nonebot.adapters.matrix.bot import Bot
from nonebot.adapters.matrix.config import BotInfo, Config

from nonebot.drivers import Request, Response


class DummyAdapter(Adapter, HandleMixin):
    def __init__(self, *, status_code: int = 200, content: bytes = b"{}") -> None:
        self.matrix_config = Config()
        self.status_code = status_code
        self.content = content
        self.request_calls: list[Request] = []

    @override
    @staticmethod
    def get_authorization(bot_info: BotInfo) -> str:
        return f"Bearer {bot_info.access_token}"

    @override
    async def request(self, setup: Request) -> Response:
        self.request_calls.append(setup)
        return Response(self.status_code, content=self.content)


class DummyBot(Bot):
    def __init__(
        self,
        adapter: DummyAdapter | None = None,
        *,
        homeserver: str = "https://matrix.example.org",
        access_token: str = "test-token",  # noqa: S107
        user_id: str = "@bot:example.org",
    ) -> None:
        if adapter is None:
            adapter = DummyAdapter()
        bot_info = BotInfo(
            homeserver=homeserver,
            access_token=access_token,
            user_id=user_id,
        )
        super().__init__(
            adapter=adapter,
            self_id=user_id,
            bot_info=bot_info,
            self_info=WhoamiResponse(user_id=user_id),
        )
