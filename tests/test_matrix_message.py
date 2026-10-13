import json

from nonebot.adapters.matrix.api.model import SyncResponse
from nonebot.adapters.matrix.message import (
    Message,
    MessageSegment,
    build_edit_content,
    build_message_content,
    message_from_content,
    parse_message,
)
from tests.fake.doubles import DummyAdapter, DummyBot

from nonebot.compat import type_validate_python
import pytest


def test_convert_message_from_api_json() -> None:
    content = b'{"next_batch":"s7055362852_757284974_4340430_4776043867_5561134979_269490140_1633051276_11314874994_0_703827_34_1_1360266","device_one_time_keys_count":{"signed_curve25519":47},"device_unused_fallback_key_types":["signed_curve25519"],"rooms":{"join":{"!PPaIWnOCgdZmRMfpXH:matrix.debian.social":{"timeline":{"events":[{"unsigned":{"membership":"join"},"content":{"body":"<html><h1>\\u6807\\u9898</h1></html>","m.mentions":{},"msgtype":"m.text"},"origin_server_ts":1781085955184,"sender":"@qinyn:matrix.debian.social","type":"m.room.message","event_id":"$6Wjghb7JNLkT6wUEXT8lCoM9uIlNF-ULsH9xvo3LCvg"}],"prev_batch":"s7055362792_757284974_4340430_4776043867_5561134979_269490140_1633051276_11314874994_0_703827_34_1_1360266","limited":false},"state":{"events":[]},"account_data":{"events":[]},"ephemeral":{"events":[{"type":"m.typing","content":{"user_ids":[]}},{"type":"m.receipt","content":{"$Dd_jVxydeOyh3kDjAcM15gYGKMpI8B3sHuE4OAs5l6I":{"m.read":{"@qinyn:matrix.debian.social":{"thread_id":"main","ts":1781085951796},"@zinface:mozilla.org":{"thread_id":"main","ts":1781085951947}}}}}]},"unread_notifications":{"notification_count":19,"highlight_count":0},"summary":{}}}}}'

    data = json.loads(content)
    sync = type_validate_python(SyncResponse, data)

    assert sync.next_batch.startswith("s7055362852")
    assert sync.device_one_time_keys_count == {"signed_curve25519": 47}

    room = sync.rooms.join["!PPaIWnOCgdZmRMfpXH:matrix.debian.social"]
    assert room.unread_notifications == {"notification_count": 19, "highlight_count": 0}

    event = room.timeline.events[0]
    assert event.type == "m.room.message"
    assert event.event_id == "$6Wjghb7JNLkT6wUEXT8lCoM9uIlNF-ULsH9xvo3LCvg"
    assert event.sender == "@qinyn:matrix.debian.social"
    assert event.origin_server_ts == 1781085955184
    assert event.content["msgtype"] == "m.text"
    # JSON \u escapes are decoded by json.loads into actual Unicode
    assert event.content["body"] == "<html><h1>标题</h1></html>"

    message = message_from_content(event.content)
    assert message[0].type == "text"
    assert message.extract_plain_text() == "<html><h1>标题</h1></html>"


@pytest.mark.asyncio
async def test_text_and_mention_build_matrix_content(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"event_id":"$sent:example.org"}'
    dummy_bot.adapter.responses = [(200, b"[]")]
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
    assert payload["info"]["mimetype"] == "image/png"


@pytest.mark.asyncio
async def test_video_and_webp_media_include_mimetypes(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"content_uri":"mxc://example.org/media"}'

    video = await build_message_content(
        MessageSegment.video(b"video-bytes", filename="clip.mp4"), bot=dummy_bot
    )
    webp = await build_message_content(
        MessageSegment.image(b"webp-bytes", filename="sticker.webp"), bot=dummy_bot
    )

    assert video["msgtype"] == "m.video"
    assert video["info"]["mimetype"] == "video/mp4"
    assert webp["msgtype"] == "m.image"
    assert webp["info"]["mimetype"] == "image/webp"


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

    edited = build_edit_content(MessageSegment.text("changed"), "$old:example.org")
    assert edited == {
        "msgtype": "m.text",
        "body": "* changed",
        "m.new_content": {
            "msgtype": "m.text",
            "body": "changed",
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$old:example.org"},
    }


@pytest.mark.asyncio
async def test_api_sync_parses_response() -> None:
    content = b'{"next_batch":"s7055362852_757284974_4340430_4776043867_5561134979_269490140_1633051276_11314874994_0_703827_34_1_1360266","device_one_time_keys_count":{"signed_curve25519":47},"device_unused_fallback_key_types":["signed_curve25519"],"rooms":{"join":{"!PPaIWnOCgdZmRMfpXH:matrix.debian.social":{"timeline":{"events":[{"unsigned":{"membership":"join"},"content":{"body":"<html><h1>\\u6807\\u9898</h1></html>","m.mentions":{},"msgtype":"m.text"},"origin_server_ts":1781085955184,"sender":"@qinyn:matrix.debian.social","type":"m.room.message","event_id":"$6Wjghb7JNLkT6wUEXT8lCoM9uIlNF-ULsH9xvo3LCvg"}],"prev_batch":"s7055362792_757284974_4340430_4776043867_5561134979_269490140_1633051276_11314874994_0_703827_34_1_1360266","limited":false},"state":{"events":[]},"account_data":{"events":[]},"ephemeral":{"events":[{"type":"m.typing","content":{"user_ids":[]}},{"type":"m.receipt","content":{"$Dd_jVxydeOyh3kDjAcM15gYGKMpI8B3sHuE4OAs5l6I":{"m.read":{"@qinyn:matrix.debian.social":{"thread_id":"main","ts":1781085951796},"@zinface:mozilla.org":{"thread_id":"main","ts":1781085951947}}}}}]},"unread_notifications":{"notification_count":19,"highlight_count":0},"summary":{}}}}}'

    adapter = DummyAdapter(content=content)
    bot = DummyBot(adapter=adapter)

    sync = await adapter._api_sync(bot)

    # Verify the request
    assert len(adapter.request_calls) == 1
    assert adapter.request_calls[0].method == "GET"
    assert str(adapter.request_calls[0].url).endswith("/_matrix/client/v3/sync")

    # Verify the response
    assert sync.next_batch.startswith("s7055362852")
    assert sync.device_one_time_keys_count == {"signed_curve25519": 47}

    room = sync.rooms.join["!PPaIWnOCgdZmRMfpXH:matrix.debian.social"]
    event = room.timeline.events[0]
    assert event.type == "m.room.message"
    assert event.sender == "@qinyn:matrix.debian.social"
    assert event.content["msgtype"] == "m.text"
    assert event.content["body"] == "<html><h1>标题</h1></html>"


def test_message_from_matrix_content() -> None:
    message = message_from_content({"msgtype": "m.emote", "body": "waves"})
    assert message[0].type == "emote"
    assert message.extract_plain_text() == "waves"

    image = message_from_content(
        {"msgtype": "m.image", "body": "pic", "url": "mxc://example.org/pic"}
    )
    assert image[0].type == "image"
    assert image[0].data["url"] == "mxc://example.org/pic"


def test_sticker_message_segment_round_trip() -> None:
    segment = MessageSegment.sticker(
        "mxc://example.org/sticker",
        body="sticker.webp",
        content_type="image/webp",
        info={"w": 128, "h": 128},
    )
    content = parse_message(segment)
    assert content == {
        "msgtype": "m.sticker",
        "body": "sticker.webp",
        "url": "mxc://example.org/sticker",
        "info": {"w": 128, "h": 128, "mimetype": "image/webp"},
    }
    restored = message_from_content(content)
    assert restored[0].type == "sticker"
    assert restored[0].data["url"] == "mxc://example.org/sticker"


def test_location_message_keeps_raw_payload() -> None:
    content = {
        "msgtype": "m.location",
        "body": "Office",
        "geo_uri": "geo:37.786971,-122.399677",
        "org.matrix.msc3488.location": {"description": "Office"},
    }
    message = message_from_content(content)
    assert message[0].type == "raw"
    assert message[0].data["content"] == content


def test_unknown_message_type_becomes_raw() -> None:
    content = {"msgtype": "m.custom", "body": "hi", "extra": 1}
    message = message_from_content(content)
    assert message[0].type == "raw"
    assert message[0].data["content"] == content
    assert message[0].data["event_type"] == "m.room.message"
