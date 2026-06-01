from nonebot.adapters.matrix.message import (
    Message,
    MessageSegment,
    build_message_content,
    message_from_content,
    parse_message,
)
from tests.fake.doubles import DummyBot

import pytest


@pytest.mark.asyncio
async def test_text_and_mention_build_matrix_content(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"event_id":"$sent:example.org"}'
    message = Message(
        [
            MessageSegment.text("hi "),
            MessageSegment.mention_user("@alice:example.org", "Alice"),
        ]
    )

    response = await dummy_bot.send_to("!room:example.org", message, txn_id="txn1")

    request = dummy_bot.adapter.request_calls[-1]
    assert request.json["msgtype"] == "m.text"
    assert request.json["body"] == "hi Alice"
    assert request.json["format"] == "org.matrix.custom.html"
    assert request.json["m.mentions"] == {"user_ids": ["@alice:example.org"]}
    assert response.event_id == "$sent:example.org"


@pytest.mark.asyncio
async def test_media_segment_uploads_before_send(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"content_uri":"mxc://example.org/image"}'

    payload = await build_message_content(
        MessageSegment.image(
            b"image-bytes",
            filename="image.png",
            content_type="image/png",
        ),
        bot=dummy_bot,
    )

    upload_request = dummy_bot.adapter.request_calls[-1]
    assert upload_request.content == b"image-bytes"
    assert payload["msgtype"] == "m.image"
    assert payload["url"] == "mxc://example.org/image"


def test_parse_text_notice_html_and_reply_content() -> None:
    payload = parse_message(
        Message(
            [
                MessageSegment.reply("$event:example.org"),
                MessageSegment.notice("heads up"),
            ]
        )
    )

    assert payload == {
        "msgtype": "m.notice",
        "body": "heads up",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$event:example.org"}},
    }

    html = parse_message(MessageSegment.html("bold", "<strong>bold</strong>"))
    assert html["format"] == "org.matrix.custom.html"
    assert html["formatted_body"] == "<strong>bold</strong>"


def test_message_from_matrix_content() -> None:
    message = message_from_content({"msgtype": "m.emote", "body": "waves"})
    assert message[0].type == "emote"
    assert message.extract_plain_text() == "waves"

    image = message_from_content(
        {"msgtype": "m.image", "body": "pic", "url": "mxc://example.org/pic"}
    )
    assert image[0].type == "image"
    assert image[0].data["url"] == "mxc://example.org/pic"
