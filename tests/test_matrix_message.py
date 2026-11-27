import json

from nonebot.adapters.matrix.api.model import SyncResponse
from nonebot.adapters.matrix.message import (
    Message,
    MessageSegment,
    add_reply_fallback,
    build_edit_content,
    build_message_content,
    message_from_content,
    parse_message,
)
from tests.fake.doubles import DummyAdapter, DummyBot

from nonebot.compat import type_validate_python
import pytest


def test_convert_message_from_api_json() -> None:
    content = b'{"next_batch":"s123_456","device_one_time_keys_count":{"signed_curve25519":47},"device_unused_fallback_key_types":["signed_curve25519"],"rooms":{"join":{"!room:example.org":{"timeline":{"events":[{"unsigned":{"membership":"join"},"content":{"body":"<html><h1>\\u6807\\u9898</h1></html>","m.mentions":{},"msgtype":"m.text"},"origin_server_ts":1700000000000,"sender":"@alice:example.org","type":"m.room.message","event_id":"$event:example.org"}],"prev_batch":"s123_455","limited":false},"state":{"events":[]},"account_data":{"events":[]},"ephemeral":{"events":[{"type":"m.typing","content":{"user_ids":[]}},{"type":"m.receipt","content":{"$receipt:example.org":{"m.read":{"@alice:example.org":{"thread_id":"main","ts":1699999999000},"@bob:example.org":{"thread_id":"main","ts":1699999999500}}}}}]},"unread_notifications":{"notification_count":19,"highlight_count":0},"summary":{}}}}}'

    data = json.loads(content)
    sync = type_validate_python(SyncResponse, data)

    assert sync.next_batch.startswith("s123_456")
    assert sync.device_one_time_keys_count == {"signed_curve25519": 47}

    room = sync.rooms.join["!room:example.org"]
    assert room.unread_notifications == {"notification_count": 19, "highlight_count": 0}

    event = room.timeline.events[0]
    assert event.type == "m.room.message"
    assert event.event_id == "$event:example.org"
    assert event.sender == "@alice:example.org"
    assert event.origin_server_ts == 1700000000000
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
async def test_room_mention_builds_matrix_content(dummy_bot: DummyBot) -> None:
    dummy_bot.adapter.content = b'{"event_id":"$sent:example.org"}'
    dummy_bot.adapter.responses = [(200, b"[]")]

    await dummy_bot.send_to(
        "!room:example.org",
        Message([MessageSegment.mention_room(), MessageSegment.text(" hello")]),
        txn_id="txn-room-mention",
    )

    request = dummy_bot.adapter.request_calls[-1]
    assert request.json["body"] == "@room hello"
    assert request.json["m.mentions"] == {"room": True}
    assert request.json["formatted_body"] == "@room hello"


@pytest.mark.asyncio
async def test_send_reply_fetches_target_and_adds_fallback(
    dummy_bot: DummyBot,
) -> None:
    room_id = "!room:example.org"
    dummy_bot._encryption_states[room_id] = None
    dummy_bot.adapter.responses = [
        (
            200,
            b'{"type":"m.room.message","event_id":"$target:example.org",'
            b'"sender":"@alice:example.org","content":{"msgtype":"m.text",'
            b'"body":"quoted"}}',
        ),
        (200, b'{"event_id":"$sent:example.org"}'),
    ]

    await dummy_bot.send_to(
        room_id,
        Message(
            [MessageSegment.reply("$target:example.org"), MessageSegment.text("answer")]
        ),
        txn_id="txn-reply",
    )

    target_request, send_request = dummy_bot.adapter.request_calls[-2:]
    assert target_request.method == "GET"
    assert target_request.url.raw_path.endswith(
        "/rooms/%21room%3Aexample.org/event/%24target%3Aexample.org"
    )
    assert send_request.json["body"] == "> <@alice:example.org> quoted\n\nanswer"
    assert "<mx-reply>" in send_request.json["formatted_body"]
    assert (
        send_request.json["m.relates_to"]["m.in_reply_to"]["event_id"]
        == "$target:example.org"
    )


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


@pytest.mark.asyncio
async def test_voice_message_uploads_and_builds_matrix_voice_payload(
    dummy_bot: DummyBot,
) -> None:
    dummy_bot.adapter.content = b'{"content_uri":"mxc://example.org/voice"}'
    payload = await build_message_content(
        MessageSegment.voice(
            b"voice-bytes",
            duration=952,
            waveform=[0.0, 0.5, 1.0, -0.1, 1.2],
        ),
        bot=dummy_bot,
    )

    upload_request = dummy_bot.adapter.request_calls[-1]
    assert upload_request.content == b"voice-bytes"
    assert payload == {
        "msgtype": "m.audio",
        "body": "Voice message",
        "url": "mxc://example.org/voice",
        "info": {
            "mimetype": "audio/ogg",
            "duration": 952,
            "size": len(b"voice-bytes"),
        },
        "org.matrix.msc1767.file": {
            "name": "Voice message.ogg",
            "mimetype": "audio/ogg",
            "size": len(b"voice-bytes"),
            "url": "mxc://example.org/voice",
        },
        "org.matrix.msc1767.text": "Voice message",
        "org.matrix.msc3245.voice": {},
        "org.matrix.msc1767.audio": {
            "duration": 952,
            "waveform": [0, 512, 1024, 0, 1024],
        },
    }


def test_voice_message_can_reference_existing_mxc_uri() -> None:
    payload = parse_message(
        MessageSegment.voice("mxc://example.org/voice", duration=1234)
    )

    assert payload["msgtype"] == "m.audio"
    assert payload["url"] == "mxc://example.org/voice"
    assert payload["org.matrix.msc3245.voice"] == {}
    assert "org.matrix.msc1767.audio" not in payload


def test_message_from_content_restores_voice_message_metadata() -> None:
    message = message_from_content(
        {
            "msgtype": "m.audio",
            "body": "Voice message",
            "url": "mxc://matrix.debian.social/gYkWMWpNZjnXHzvxpmeuZjyz",
            "info": {"duration": 952, "mimetype": "audio/ogg", "size": 1396},
            "org.matrix.msc1767.audio": {
                "duration": 952,
                "waveform": [0, 512, 1024],
            },
            "org.matrix.msc1767.file": {
                "name": "Voice message.ogg",
                "mimetype": "audio/ogg",
                "size": 1396,
                "url": "mxc://matrix.debian.social/gYkWMWpNZjnXHzvxpmeuZjyz",
            },
            "org.matrix.msc1767.text": "Voice message",
            "org.matrix.msc3245.voice": {},
        }
    )

    assert len(message) == 1
    segment = message[0]
    assert segment.type == "voice"
    assert segment.data["url"] == "mxc://matrix.debian.social/gYkWMWpNZjnXHzvxpmeuZjyz"
    assert segment.data["filename"] == "Voice message.ogg"
    assert segment.data["body"] == "Voice message"
    assert segment.data["duration"] == 952
    assert segment.data["waveform"] == [0.0, 0.5, 1.0]
    assert segment.data["content_type"] == "audio/ogg"
    assert segment.data["info"]["size"] == 1396


def test_message_from_content_keeps_regular_audio_as_audio_segment() -> None:
    message = message_from_content(
        {
            "msgtype": "m.audio",
            "body": "recording.ogg",
            "url": "mxc://example.org/recording",
        }
    )

    assert message[0].type == "audio"


def test_message_from_content_preserves_encrypted_voice_file() -> None:
    encrypted_file = {
        "url": "mxc://example.org/encrypted-voice",
        "key": {"kty": "oct", "k": "key"},
        "iv": "iv",
        "hashes": {"sha256": "hash"},
        "v": "v2",
    }
    message = message_from_content(
        {
            "msgtype": "m.audio",
            "body": "Voice message",
            "org.matrix.msc1767.file": {
                "name": "Voice message.ogg",
                "mimetype": "audio/ogg",
                "file": encrypted_file,
            },
            "org.matrix.msc3245.voice": {},
        }
    )

    assert message[0].type == "voice"
    assert message[0].data["url"] is None
    assert message[0].data["file"] == encrypted_file


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

    room_mention = parse_message(
        Message([MessageSegment.mention_room(), MessageSegment.text(" hello")])
    )
    assert room_mention["body"] == "@room hello"
    assert room_mention["m.mentions"] == {"room": True}

    combined_mentions = parse_message(
        Message(
            [
                MessageSegment.mention_room(),
                MessageSegment.text(" "),
                MessageSegment.mention_user("@alice:example.org", "Alice"),
            ]
        )
    )
    assert combined_mentions["m.mentions"] == {
        "room": True,
        "user_ids": ["@alice:example.org"],
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


def test_message_from_content_strips_reply_fallbacks() -> None:
    content = {
        "msgtype": "m.text",
        "body": "> <@alice:example.org> old text\n\n.zssm",
        "format": "org.matrix.custom.html",
        "formatted_body": (
            "<mx-reply><blockquote>quoted</blockquote></mx-reply><p>.zssm</p>"
        ),
        "m.relates_to": {"m.in_reply_to": {"event_id": "$event:example.org"}},
    }

    message = message_from_content(content)

    assert message.extract_plain_text() == ".zssm"
    assert message[0].data["body"] == ".zssm"
    assert message[0].data["formatted_body"] == "<p>.zssm</p>"


def test_reply_without_relation_keeps_fallback_text() -> None:
    message = message_from_content({"msgtype": "m.text", "body": "> quoted\n\ntext"})

    assert message.extract_plain_text() == "> quoted\n\ntext"


def test_add_reply_fallback_to_message_payload() -> None:
    content = {
        "msgtype": "m.text",
        "body": "reply",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$target:example.org"}},
    }
    target = {
        "type": "m.room.message",
        "event_id": "$target:example.org",
        "sender": "@alice:example.org",
        "content": {"msgtype": "m.text", "body": "quoted <text>"},
    }

    add_reply_fallback(content, target, room_id="!room:example.org")

    assert content["body"] == ("> <@alice:example.org> quoted <text>\n\nreply")
    assert content["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "$target:example.org"}
    }
    assert "<mx-reply>" in content["formatted_body"]
    assert "quoted &lt;text&gt;" in content["formatted_body"]


@pytest.mark.asyncio
async def test_api_sync_parses_response() -> None:
    content = b'{"next_batch":"s123_456","device_one_time_keys_count":{"signed_curve25519":47},"device_unused_fallback_key_types":["signed_curve25519"],"rooms":{"join":{"!room:example.org":{"timeline":{"events":[{"unsigned":{"membership":"join"},"content":{"body":"<html><h1>\\u6807\\u9898</h1></html>","m.mentions":{},"msgtype":"m.text"},"origin_server_ts":1700000000000,"sender":"@alice:example.org","type":"m.room.message","event_id":"$event:example.org"}],"prev_batch":"s123_455","limited":false},"state":{"events":[]},"account_data":{"events":[]},"ephemeral":{"events":[{"type":"m.typing","content":{"user_ids":[]}},{"type":"m.receipt","content":{"$receipt:example.org":{"m.read":{"@alice:example.org":{"thread_id":"main","ts":1699999999000},"@bob:example.org":{"thread_id":"main","ts":1699999999500}}}}}]},"unread_notifications":{"notification_count":19,"highlight_count":0},"summary":{}}}}}'

    adapter = DummyAdapter(content=content)
    bot = DummyBot(adapter=adapter)

    sync = await adapter._api_sync(bot)

    # Verify the request
    assert len(adapter.request_calls) == 1
    assert adapter.request_calls[0].method == "GET"
    assert str(adapter.request_calls[0].url).endswith("/_matrix/client/v3/sync")

    # Verify the response
    assert sync.next_batch.startswith("s123_456")
    assert sync.device_one_time_keys_count == {"signed_curve25519": 47}

    room = sync.rooms.join["!room:example.org"]
    event = room.timeline.events[0]
    assert event.type == "m.room.message"
    assert event.sender == "@alice:example.org"
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
        "geo_uri": "geo:12.345678,23.456789",
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
