from typing import Any, TypeAlias

from nonebot.compat import PYDANTIC_V2
from nonebot.utils import logger_wrapper
from pydantic import BaseModel

from .api.types import UNSET

if PYDANTIC_V2:
    from pydantic.main import IncEx
else:
    IncEx: TypeAlias = (
        set[int]
        | set[str]
        | dict[int, "IncEx | bool"]
        | dict[str, "IncEx | bool"]
        | None
    )


_logger = logger_wrapper("Matrix")
_log_level = "INFO"


def configure_log_level(level: str) -> None:
    global _log_level  # noqa: PLW0603
    _log_level = level


def log(level: str, message: str, exception: Exception | None = None) -> None:
    if level == "ERROR" and _log_level != "DEBUG":
        exception = None
    _logger(level, message, exception)


def omit_unset(data: Any) -> Any:  # noqa: ANN401
    if isinstance(data, dict):
        return data.__class__(
            (key, omit_unset(value))
            for key, value in data.items()
            if value is not UNSET
        )
    if isinstance(data, (list, tuple, set)):
        return data.__class__(omit_unset(item) for item in data if item is not UNSET)
    return data


def model_dump(  # noqa: PLR0913
    model: BaseModel,
    include: IncEx | None = None,
    exclude: IncEx | None = None,
    *,
    by_alias: bool = False,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    omit_unset_values: bool = False,
) -> dict[str, Any]:
    if PYDANTIC_V2:
        data = model.model_dump(
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
    else:
        data = model.dict(
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
    if omit_unset_values:
        data = omit_unset(data)
    return data


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
