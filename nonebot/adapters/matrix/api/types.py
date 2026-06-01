from typing import TypeAlias, TypeVar
from typing_extensions import Self, override

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema


class _Unset:
    @override
    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()
T = TypeVar("T")
Missing: TypeAlias = T | _Unset
MissingOrNullable: TypeAlias = T | None | _Unset


class MatrixId(str):
    __slots__ = ()
    sigil: str = ""

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str):
            msg = f"{cls.__name__} must be a string"
            raise TypeError(msg)
        if cls.sigil and not value.startswith(cls.sigil):
            msg = f"{cls.__name__} must start with {cls.sigil!r}"
            raise ValueError(msg)
        return str.__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type, handler
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


class UserId(MatrixId):
    sigil = "@"


class RoomId(MatrixId):
    sigil = "!"


class RoomAlias(MatrixId):
    sigil = "#"


class EventId(MatrixId):
    sigil = "$"


class MxcUri(str):
    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str):
            msg = "MxcUri must be a string"
            raise TypeError(msg)
        if not value.startswith("mxc://"):
            msg = "MxcUri must start with 'mxc://'"
            raise ValueError(msg)
        return str.__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        del source_type, handler
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )


DeviceId: TypeAlias = str
TxnId: TypeAlias = str
RoomIdentifier: TypeAlias = RoomId | RoomAlias | str

PresenceState: TypeAlias = str
ReceiptType: TypeAlias = str
EventType: TypeAlias = str
MessageType: TypeAlias = str


def is_unset(value: object) -> bool:
    return value is UNSET


def is_not_unset(value: object) -> bool:
    return value is not UNSET


__all__ = (
    "UNSET",
    "DeviceId",
    "EventId",
    "EventType",
    "MatrixId",
    "MessageType",
    "Missing",
    "MissingOrNullable",
    "MxcUri",
    "PresenceState",
    "ReceiptType",
    "RoomAlias",
    "RoomId",
    "RoomIdentifier",
    "TxnId",
    "UserId",
    "is_not_unset",
    "is_unset",
)
