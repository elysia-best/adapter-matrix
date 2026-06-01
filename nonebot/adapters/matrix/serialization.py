from dataclasses import dataclass
import json
from typing import Any, TypeAlias, TypedDict

from nonebot.compat import PYDANTIC_V2
from nonebot.internal.driver import FileTypes
from pydantic import BaseModel

from .api.model import File
from .utils import IncEx, model_dump

if PYDANTIC_V2:
    from pydantic import TypeAdapter
else:
    from pydantic.json import pydantic_encoder

if PYDANTIC_V2:
    _JSON_ADAPTER = TypeAdapter(Any)

MultipartFormData: TypeAlias = dict[str, FileTypes]


class JsonTransportRequest(TypedDict):
    json: dict[str, Any]


class MultipartTransportRequest(TypedDict):
    files: MultipartFormData


EncodedPreparedRequest: TypeAlias = JsonTransportRequest | MultipartTransportRequest


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    body: BaseModel
    files: list[File] | None = None
    include: IncEx | None = None
    exclude: IncEx | None = None
    by_alias: bool = False
    exclude_unset: bool = False
    exclude_defaults: bool = False
    exclude_none: bool = False
    omit_unset_values: bool = False


def prepare_request(  # noqa: PLR0913
    body: BaseModel,
    *,
    files: list[File] | None = None,
    include: IncEx | None = None,
    exclude: IncEx | None = None,
    by_alias: bool = False,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    omit_unset_values: bool = False,
) -> PreparedRequest:
    return PreparedRequest(
        body=body,
        files=files,
        include=include,
        exclude=exclude,
        by_alias=by_alias,
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
        exclude_none=exclude_none,
        omit_unset_values=omit_unset_values,
    )


def encode_json_text(value: object) -> str:
    if PYDANTIC_V2:
        return _JSON_ADAPTER.dump_json(value).decode()
    return json.dumps(value, default=pydantic_encoder, separators=(",", ":"))


def encode_model_json_text(  # noqa: PLR0913
    model: BaseModel,
    include: IncEx | None = None,
    exclude: IncEx | None = None,
    *,
    by_alias: bool = False,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    omit_unset_values: bool = False,
) -> str:
    payload = model_dump(
        model,
        include=include,
        exclude=exclude,
        by_alias=by_alias,
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
        exclude_none=exclude_none,
        omit_unset_values=omit_unset_values,
    )
    return encode_json_text(payload)


def encode_model_json_data(  # noqa: PLR0913
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
    payload = model_dump(
        model,
        include=include,
        exclude=exclude,
        by_alias=by_alias,
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
        exclude_none=exclude_none,
        omit_unset_values=omit_unset_values,
    )
    if PYDANTIC_V2:
        json_payload = _JSON_ADAPTER.dump_python(payload, mode="json")
        if not isinstance(json_payload, dict):
            msg = "transport JSON encoding must produce a mapping"
            raise TypeError(msg)
        return json_payload
    return json.loads(encode_json_text(payload))


def _build_multipart_payload(files: list[File]) -> MultipartFormData:
    multipart: MultipartFormData = {}
    for index, file in enumerate(files):
        if file.content_type:
            multipart[f"file[{index}]"] = (
                file.filename,
                file.content,
                file.content_type,
            )
        else:
            multipart[f"file[{index}]"] = (file.filename, file.content)
    return multipart


def encode_prepared_request(prepared: PreparedRequest) -> EncodedPreparedRequest:
    if prepared.files:
        return {"files": _build_multipart_payload(prepared.files)}
    payload = encode_model_json_data(
        prepared.body,
        include=prepared.include,
        exclude=prepared.exclude,
        by_alias=prepared.by_alias,
        exclude_unset=prepared.exclude_unset,
        exclude_defaults=prepared.exclude_defaults,
        exclude_none=prepared.exclude_none,
        omit_unset_values=prepared.omit_unset_values,
    )
    return {"json": payload}
