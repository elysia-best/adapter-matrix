from typing import Any, Literal

from pydantic import BaseModel, Field


class BotInfo(BaseModel):
    homeserver: str
    access_token: str
    user_id: str | None = None
    device_id: str | None = None
    sync_filter: str | dict[str, Any] | None = None
    set_presence: Literal["online", "offline", "unavailable"] | None = None


class Config(BaseModel):
    matrix_bots: list[BotInfo] = Field(default_factory=list)
    matrix_api_timeout: float = 30.0
    matrix_sync_timeout: int = 30000
    matrix_retry_interval: float = 3.0
    matrix_handle_self_message: bool = False
    matrix_proxy: str | None = None
