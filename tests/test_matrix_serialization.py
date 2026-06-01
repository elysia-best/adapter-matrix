from nonebot.adapters.matrix.api.model import RoomMessageContent
from nonebot.adapters.matrix.api.types import UNSET
from nonebot.adapters.matrix.serialization import encode_model_json_data
from nonebot.adapters.matrix.utils import omit_unset


def test_unset_values_are_omitted_recursively() -> None:
    assert omit_unset({"a": 1, "b": UNSET, "c": [UNSET, 2]}) == {"a": 1, "c": [2]}


def test_model_json_uses_aliases_and_preserves_none() -> None:
    model = RoomMessageContent(
        msgtype="m.text",
        body="hello",
        format=None,
        mentions={"user_ids": ["@bot:example.org"]},
    )

    data = encode_model_json_data(model, by_alias=True)

    assert data["msgtype"] == "m.text"
    assert data["format"] is None
    assert data["m.mentions"] == {"user_ids": ["@bot:example.org"]}
