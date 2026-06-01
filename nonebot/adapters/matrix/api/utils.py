from typing import Any

from nonebot.compat import type_validate_python
from pydantic import BaseModel

from .types import UNSET
from ..serialization import PreparedRequest, prepare_request


def parse_data(data: dict[str, Any], model_class: type[BaseModel]) -> PreparedRequest:
    model = type_validate_python(model_class, data)
    return prepare_request(model, by_alias=True, omit_unset_values=True)


def filter_unset_query(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if value is not UNSET and value is not None
    }
