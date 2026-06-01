import json
from typing import Any
from typing_extensions import override

from nonebot.drivers import Response
from nonebot.exception import (
    ActionFailed as BaseActionFailed,
    AdapterException,
    ApiNotAvailable as BaseApiNotAvailable,
    NetworkError as BaseNetworkError,
    NoLogException as BaseNoLogException,
)


class MatrixAdapterException(AdapterException):
    def __init__(self) -> None:
        super().__init__("Matrix")


class NoLogException(BaseNoLogException, MatrixAdapterException):
    pass


class ActionFailed(BaseActionFailed, MatrixAdapterException):
    def __init__(self, response: Response) -> None:
        self.status_code = response.status_code
        self.errcode: str | None = None
        self.message: str | None = None
        self.retry_after_ms: int | None = None
        self.body: dict[str, Any] | None = None
        if response.content:
            try:
                body = json.loads(response.content)
            except json.JSONDecodeError:
                content = response.content
                error = (
                    content.decode(errors="replace")
                    if isinstance(content, bytes)
                    else str(content)
                )
                body = {"error": error}
            if isinstance(body, dict):
                self._prepare_body(body)

    @override
    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}: {self.status_code}, "
            f"errcode={self.errcode}, message={self.message}, "
            f"retry_after_ms={self.retry_after_ms}>"
        )

    @override
    def __str__(self) -> str:
        return self.__repr__()

    def _prepare_body(self, body: dict[str, Any]) -> None:
        self.body = body
        self.errcode = body.get("errcode")
        self.message = body.get("error")
        self.retry_after_ms = body.get("retry_after_ms")


class UnauthorizedException(ActionFailed):
    pass


class RateLimitException(ActionFailed):
    pass


class NetworkError(BaseNetworkError, MatrixAdapterException):
    def __init__(self, msg: str | None = None) -> None:
        super().__init__()
        self.msg = msg

    @override
    def __repr__(self) -> str:
        return f"<NetworkError message={self.msg}>"

    @override
    def __str__(self) -> str:
        return self.__repr__()


class ApiNotAvailable(BaseApiNotAvailable, MatrixAdapterException):
    pass
