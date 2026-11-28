from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import mimetypes
import re
from typing import Any, Protocol, cast
from typing_extensions import Self, override

from nonebot.adapters import (
    Message as BaseMessage,
    MessageSegment as BaseMessageSegment,
)

from nonebot.compat import type_validate_python
from nonebot.utils import escape_tag

from .api.model import UploadResponse
from .api.types import EventId, MxcUri, UserId
from .utils import escape, log, unescape

MATRIX_HTML_FORMAT = "org.matrix.custom.html"
_REPLY_FALLBACK_RE = re.compile(
    r"\A\s*<mx-reply\b[^>]*>.*?</mx-reply>\s*", re.IGNORECASE | re.DOTALL
)
_PLAIN_REPLY_FALLBACK_RE = re.compile(r"\A(?:>[^\r\n]*(?:\r?\n|\r))+\r?\n")
MEDIA_SEGMENT_TYPES = {"image", "file", "audio", "video", "sticker"}
VOICE_SEGMENT_TYPE = "voice"
FALLBACK_MEDIA_TYPES = {
    "avif": "image/avif",
    "webp": "image/webp",
    "m4v": "video/mp4",
    "mkv": "video/x-matroska",
    "mov": "video/quicktime",
    "webm": "video/webm",
}
SEGMENT_MSGTYPE_MAP = {
    "text": "m.text",
    "notice": "m.notice",
    "emote": "m.emote",
    "image": "m.image",
    "file": "m.file",
    "audio": "m.audio",
    "video": "m.video",
    "sticker": "m.sticker",
}
MSGTYPE_SEGMENT_MAP = {value: key for key, value in SEGMENT_MSGTYPE_MAP.items()}


class MediaUploader(Protocol):
    async def upload_media(
        self,
        content: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> UploadResponse: ...


class EncryptedMediaUploader(MediaUploader, Protocol):
    async def upload_encrypted_media(self, content: bytes) -> dict[str, Any]: ...


class MessageSegment(BaseMessageSegment["Message"]):
    @classmethod
    @override
    def get_message_class(cls) -> type[Message]:
        return Message

    @staticmethod
    def text(content: str) -> TextSegment:
        return TextSegment("text", {"text": content})

    @staticmethod
    def notice(content: str) -> NoticeSegment:
        return NoticeSegment("notice", {"text": content})

    @staticmethod
    def emote(content: str) -> EmoteSegment:
        return EmoteSegment("emote", {"text": content})

    @staticmethod
    def html(body: str, formatted_body: str) -> HtmlSegment:
        return HtmlSegment("html", {"body": body, "formatted_body": formatted_body})

    @staticmethod
    def mention_user(
        user_id: str | UserId,
        display_name: str | None = None,
    ) -> MentionUserSegment:
        return MentionUserSegment(
            "mention_user",
            {"user_id": UserId(str(user_id)), "display_name": display_name},
        )

    @staticmethod
    def mention_room() -> RoomMentionSegment:
        return RoomMentionSegment("mention_room", {})

    @staticmethod
    def reply(event_id: str | EventId) -> ReplySegment:
        return ReplySegment("reply", {"event_id": EventId(str(event_id))})

    @staticmethod
    def image(
        content: bytes | str | MxcUri,
        *,
        filename: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        return _media_segment("image", content, filename, body, content_type, info)

    @staticmethod
    def file(
        content: bytes | str | MxcUri,
        *,
        filename: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        return _media_segment("file", content, filename, body, content_type, info)

    @staticmethod
    def audio(
        content: bytes | str | MxcUri,
        *,
        filename: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        return _media_segment("audio", content, filename, body, content_type, info)

    @staticmethod
    def voice(  # noqa: PLR0913
        content: bytes | str | MxcUri,
        *,
        duration: int | None = None,
        waveform: list[float] | None = None,
        filename: str = "Voice message.ogg",
        body: str = "Voice message",
        content_type: str = "audio/ogg",
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        segment = _media_segment(
            VOICE_SEGMENT_TYPE, content, filename, body, content_type, info
        )
        segment.data["duration"] = duration
        segment.data["waveform"] = list(waveform) if waveform is not None else None
        return segment

    @staticmethod
    def video(
        content: bytes | str | MxcUri,
        *,
        filename: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        return _media_segment("video", content, filename, body, content_type, info)

    @staticmethod
    def sticker(
        content: bytes | str | MxcUri,
        *,
        filename: str | None = None,
        body: str | None = None,
        content_type: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MediaSegment:
        return _media_segment("sticker", content, filename, body, content_type, info)

    @staticmethod
    def raw(content: dict[str, Any], event_type: str | None = None) -> RawSegment:
        return RawSegment("raw", {"content": content, "event_type": event_type})

    @override
    def is_text(self) -> bool:
        return self.type in {
            "text",
            "notice",
            "emote",
            "html",
            "mention_user",
            "mention_room",
        }

    @classmethod
    @override
    def _validate(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        if isinstance(value, MessageSegment):
            msg = f"Type {type(value)} can not be converted to {cls}"
            raise TypeError(msg)
        if not isinstance(value, dict):
            msg = f"Expected dict for MessageSegment, got {type(value)}"
            raise TypeError(msg)
        if "type" not in value:
            msg = f"Expected dict with 'type' for MessageSegment, got {value}"
            raise ValueError(msg)
        segment_type = value["type"]
        if segment_type not in SEGMENT_TYPE_MAP:
            msg = f"Invalid MessageSegment type: {segment_type}"
            raise ValueError(msg)
        target = SEGMENT_TYPE_MAP[segment_type]
        if cls is MessageSegment:
            return type_validate_python(target, value)
        if cls is target:
            return target(type=segment_type, data=value.get("data", {}))
        msg = f"Segment type {segment_type!r} can not be converted to {cls}"
        raise ValueError(msg)


@dataclass
class TextSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data["text"]


@dataclass
class NoticeSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data["text"]


@dataclass
class EmoteSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data["text"]


@dataclass
class HtmlSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data["body"]


@dataclass
class MentionUserSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data.get("display_name") or str(self.data["user_id"])


@dataclass
class RoomMentionSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return "@room"


@dataclass
class ReplySegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return ""

    @override
    def is_text(self) -> bool:
        return False


@dataclass
class MediaSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        return self.data.get("body") or self.data.get("filename") or f"<{self.type}>"

    @override
    def is_text(self) -> bool:
        return False


@dataclass
class RawSegment(MessageSegment):
    @override
    def __str__(self) -> str:
        content = self.data.get("content", {})
        body = content.get("body") if isinstance(content, dict) else None
        return body or ""

    @override
    def is_text(self) -> bool:
        return False


SEGMENT_TYPE_MAP = {
    "text": TextSegment,
    "notice": NoticeSegment,
    "emote": EmoteSegment,
    "html": HtmlSegment,
    "mention_user": MentionUserSegment,
    "mention_room": RoomMentionSegment,
    "reply": ReplySegment,
    "image": MediaSegment,
    "file": MediaSegment,
    "audio": MediaSegment,
    "video": MediaSegment,
    "sticker": MediaSegment,
    VOICE_SEGMENT_TYPE: MediaSegment,
    "raw": RawSegment,
}


def _media_segment(  # noqa: PLR0913
    segment_type: str,
    content: bytes | str | MxcUri,
    filename: str | None,
    body: str | None,
    content_type: str | None,
    info: dict[str, Any] | None,
) -> MediaSegment:
    url = (
        str(content)
        if isinstance(content, str) and content.startswith("mxc://")
        else None
    )
    raw_content = content if isinstance(content, bytes) else None
    file_name = filename or body or (str(content).rsplit("/", 1)[-1] if url else None)
    resolved_content_type = content_type or _guess_content_type(file_name)
    media_info = dict(info or {})
    if resolved_content_type and "mimetype" not in media_info:
        media_info["mimetype"] = resolved_content_type
    return MediaSegment(
        segment_type,
        {
            "content": raw_content,
            "url": url,
            "filename": file_name,
            "body": body or file_name or segment_type,
            "content_type": resolved_content_type,
            "info": media_info,
        },
    )


def _guess_content_type(filename: str | None) -> str | None:
    if not filename:
        return None
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return FALLBACK_MEDIA_TYPES.get(suffix)


class Message(BaseMessage[MessageSegment]):
    @classmethod
    @override
    def get_segment_class(cls) -> type[MessageSegment]:
        return MessageSegment

    @staticmethod
    @override
    def _construct(msg: str) -> Iterable[MessageSegment]:
        yield MessageSegment.text(msg)

    @override
    def extract_plain_text(self) -> str:
        return "".join(str(seg) for seg in self if seg.is_text())

    def clone(self) -> Message:
        return self.copy()

    def sendable(self) -> bool:
        return bool(self)


def _ensure_message(message: str | BaseMessage | BaseMessageSegment) -> Message:
    if isinstance(message, Message):
        return message
    if isinstance(message, MessageSegment):
        return Message(message)
    if isinstance(message, str):
        return Message(message)
    msg = f"Type {type(message)} can not be converted to Matrix Message"
    raise TypeError(msg)


def parse_message(message: str | BaseMessage | BaseMessageSegment) -> dict[str, Any]:
    return _build_message_content(_ensure_message(message))


def get_reply_event_id(
    message: str | BaseMessage | BaseMessageSegment,
) -> str | None:
    for segment in _ensure_message(message):
        if segment.type == "reply":
            return str(segment.data["event_id"])
    return None


def add_reply_fallback(
    content: dict[str, Any],
    target: dict[str, Any],
    *,
    room_id: str,
) -> dict[str, Any]:
    """Add Matrix plain-text and HTML reply fallbacks to a message payload."""
    target_content = target.get("content")
    if target.get("type") != "m.room.message":
        msg = "Reply target is not a room message"
        raise _reply_error(msg)
    if not isinstance(target_content, dict):
        msg = "Reply target has no message content"
        raise _reply_error(msg)
    body = target_content.get("body")
    sender = target.get("sender")
    event_id = target.get("event_id")
    if not isinstance(body, str) or not isinstance(sender, str):
        msg = "Reply target is missing its sender or body"
        raise _reply_error(msg)
    if not isinstance(event_id, str):
        msg = "Reply target is missing its event ID"
        raise _reply_error(msg)
    if is_reply_content(target_content):
        body = strip_reply_fallback(body)
    if not body:
        msg = "Reply target has no quotable message body"
        raise _reply_error(msg)

    quote = "\n".join(
        f"> <{sender}> {line}" if index == 0 else f"> {line}"
        for index, line in enumerate(body.splitlines() or [body])
    )
    quote += "\n\n"
    old_body = content.get("body", "")
    if not isinstance(old_body, str):
        msg = "Message body must be plain text"
        raise _reply_error(msg)
    content["body"] = quote + old_body

    target_link = f"https://matrix.to/#/{escape(room_id)}/{escape(event_id)}"
    sender_link = f"https://matrix.to/#/{escape(sender)}"
    fallback_html = (
        "<mx-reply><blockquote>"
        f'<a href="{target_link}">In reply to</a> '
        f'<a href="{sender_link}">{escape(sender)}</a><br>'
        f"{escape(body)}"
        "</blockquote></mx-reply>"
    )
    formatted_body = content.get("formatted_body")
    if content.get("format") == MATRIX_HTML_FORMAT and isinstance(formatted_body, str):
        content["formatted_body"] = fallback_html + formatted_body
    else:
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = (
            fallback_html + f"<p>{escape(old_body)}</p>" if old_body else fallback_html
        )
    return content


def _reply_error(message: str) -> ValueError:
    return ValueError(message)


async def build_message_content(
    message: str | BaseMessage | BaseMessageSegment,
    *,
    bot: MediaUploader | None = None,
) -> dict[str, Any]:
    return await _build_message_content_async(_ensure_message(message), bot=bot)


def build_edit_content(
    message: str | BaseMessage | BaseMessageSegment, event_id: str
) -> dict[str, Any]:
    """Build the Matrix ``m.replace`` payload for an existing message."""
    replacement = _build_message_content(_ensure_message(message))
    new_content = dict(replacement)
    new_content.pop("m.relates_to", None)
    content = {
        "msgtype": replacement.get("msgtype", "m.text"),
        "body": f"* {replacement.get('body', '')}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
    }
    if replacement.get("format") == MATRIX_HTML_FORMAT and isinstance(
        replacement.get("formatted_body"), str
    ):
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = f"* {replacement['formatted_body']}"
    return content


def _build_message_content(message: Message) -> dict[str, Any]:
    content, _ = _build_message_payload(message)
    return content


async def _build_message_content_async(
    message: Message, *, bot: MediaUploader | None
) -> dict[str, Any]:
    content, media_segment = _build_message_payload(message)
    if media_segment is None:
        return content
    content = await _materialize_media_content(content, media_segment, bot=bot)
    if media_segment.type == VOICE_SEGMENT_TYPE:
        _add_voice_message_fields(content, media_segment)
    return content


def _build_message_payload(  # noqa: C901, PLR0912, PLR0915
    message: Message,
) -> tuple[dict[str, Any], MessageSegment | None]:
    body_parts: list[str] = []
    formatted_parts: list[str] = []
    mentions: set[str] = set()
    reply_to: str | None = None
    media_segment: MessageSegment | None = None
    message_type: str | None = None
    has_html = False
    has_mention = False
    mentions_room = False

    for segment in message:
        if segment.type in {"text", "notice", "emote"}:
            text = segment.data["text"]
            body_parts.append(text)
            formatted_parts.append(escape(text))
            message_type = message_type or SEGMENT_MSGTYPE_MAP[segment.type]
        elif segment.type == "html":
            body_parts.append(segment.data["body"])
            formatted_parts.append(segment.data["formatted_body"])
            message_type = message_type or "m.text"
            has_html = True
        elif segment.type == "mention_user":
            user_id = str(segment.data["user_id"])
            display_name = segment.data.get("display_name") or user_id
            body_parts.append(display_name)
            formatted_parts.append(
                f'<a href="https://matrix.to/#/{escape(user_id)}">{escape(display_name)}</a>'
            )
            mentions.add(user_id)
            has_mention = True
        elif segment.type == "mention_room":
            body_parts.append("@room")
            formatted_parts.append("@room")
            mentions_room = True
            has_mention = True
        elif segment.type == "reply":
            reply_to = str(segment.data["event_id"])
        elif segment.type in MEDIA_SEGMENT_TYPES | {VOICE_SEGMENT_TYPE}:
            media_segment = segment
        elif segment.type == "raw":
            return dict(segment.data["content"]), None

    if media_segment is not None:
        msgtype = (
            "m.audio"
            if media_segment.type == VOICE_SEGMENT_TYPE
            else SEGMENT_MSGTYPE_MAP[media_segment.type]
        )
        content = {
            "msgtype": msgtype,
            "body": media_segment.data.get("body")
            or media_segment.data.get("filename")
            or media_segment.type,
        }
        filename = media_segment.data.get("filename")
        if (
            media_segment.type != VOICE_SEGMENT_TYPE
            and filename
            and content["body"] != filename
        ):
            content["filename"] = filename
        if media_segment.data.get("url") is not None:
            content["url"] = media_segment.data["url"]
        if media_segment.data.get("info"):
            content["info"] = media_segment.data["info"]
        _apply_relations(
            content,
            reply_to=reply_to,
            mentions=mentions,
            mentions_room=mentions_room,
        )
        if media_segment.type == VOICE_SEGMENT_TYPE:
            _add_voice_message_fields(content, media_segment)
        return content, media_segment

    body = "".join(body_parts)
    content: dict[str, Any] = {"msgtype": message_type or "m.text", "body": body}
    if has_html or has_mention:
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = "".join(formatted_parts)
    _apply_relations(
        content,
        reply_to=reply_to,
        mentions=mentions,
        mentions_room=mentions_room,
    )
    return content, None


def _add_voice_message_fields(content: dict[str, Any], segment: MessageSegment) -> None:
    """Add MSC1767 audio blocks and the MSC3245 voice marker."""
    info = dict(content.get("info") or {})
    duration = segment.data.get("duration")
    waveform = segment.data.get("waveform")
    if duration is not None:
        info["duration"] = duration
    media_bytes = segment.data.get("content")
    if isinstance(media_bytes, bytes):
        info["size"] = len(media_bytes)
    content["info"] = info

    file_block: dict[str, Any] = {
        "name": segment.data.get("filename") or "Voice message.ogg",
        "mimetype": info.get("mimetype") or "audio/ogg",
    }
    if "size" in info:
        file_block["size"] = info["size"]
    if isinstance(content.get("url"), str):
        file_block["url"] = content["url"]
    if isinstance(content.get("file"), dict):
        file_block.pop("url", None)
        file_block["file"] = content["file"]

    content["org.matrix.msc1767.file"] = file_block
    content["org.matrix.msc1767.text"] = content.get("body", "Voice message")
    content["org.matrix.msc3245.voice"] = {}

    if duration is not None and waveform is not None:
        content["org.matrix.msc1767.audio"] = {
            "duration": duration,
            "waveform": [
                int(max(0.0, min(float(value), 1.0)) * 1024) for value in waveform
            ],
        }


async def _materialize_media_content(
    content: dict[str, Any],
    segment: MessageSegment,
    *,
    bot: MediaUploader | None,
) -> dict[str, Any]:
    if segment.data.get("url") is not None:
        return content
    if bot is None:
        msg = "media segments with bytes require a bot to upload content"
        raise ValueError(msg)
    if getattr(bot, "encrypted", False) is True:
        encrypted_bot = cast("EncryptedMediaUploader", bot)
        content["file"] = await encrypted_bot.upload_encrypted_media(
            segment.data["content"]
        )
        content.pop("url", None)
        if content.get("info"):
            info = dict(content["info"])
            thumbnail = info.pop("thumbnail_bytes", None)
            if thumbnail is not None:
                info["thumbnail_file"] = await encrypted_bot.upload_encrypted_media(
                    thumbnail
                )
                info.pop("thumbnail_url", None)
            content["info"] = info
        return content
    # Matrix media is uploaded separately and then referenced via mxc://.
    uploaded = await bot.upload_media(
        segment.data["content"],
        filename=segment.data.get("filename"),
        content_type=segment.data.get("content_type"),
    )
    content["url"] = str(uploaded.content_uri)
    return content


def _apply_relations(
    content: dict[str, Any],
    *,
    reply_to: str | None,
    mentions: set[str],
    mentions_room: bool = False,
) -> None:
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    if mentions or mentions_room:
        mention_content: dict[str, Any] = {}
        if mentions:
            mention_content["user_ids"] = sorted(mentions)
        if mentions_room:
            mention_content["room"] = True
        content["m.mentions"] = mention_content


def message_from_content(content: dict[str, Any]) -> Message:  # noqa: PLR0911
    msgtype = content.get("msgtype")
    body = content.get("body", "")
    if is_reply_content(content):
        body = strip_reply_fallback(body)
        formatted_body = content.get("formatted_body")
        if isinstance(formatted_body, str):
            content = {
                **content,
                "formatted_body": strip_formatted_reply_fallback(formatted_body),
            }
    if msgtype in {"m.text", "m.notice", "m.emote"}:
        if content.get("format") == MATRIX_HTML_FORMAT and content.get(
            "formatted_body"
        ):
            return Message(MessageSegment.html(body, content["formatted_body"]))
        segment_type = MSGTYPE_SEGMENT_MAP.get(msgtype, "text")
        factory = getattr(MessageSegment, segment_type)
        log(
            "TRACE",
            f"Getting message from content with msgtype {msgtype}, "
            f"body: {escape_tag(str(body))}",
        )
        return Message(factory(body))
    if msgtype == "m.location":
        return Message(MessageSegment.raw(content, event_type="m.room.message"))
    if msgtype in {"m.image", "m.file", "m.audio", "m.video", "m.sticker"}:
        if msgtype == "m.audio" and isinstance(
            content.get("org.matrix.msc3245.voice"), dict
        ):
            return Message(_voice_segment_from_content(content, body))
        segment_type = MSGTYPE_SEGMENT_MAP[msgtype]
        factory = getattr(MessageSegment, segment_type)
        segment = factory(
            content.get("url", ""),
            filename=content.get("filename"),
            body=body,
            info=content.get("info"),
        )
        if isinstance(content.get("file"), dict):
            segment.data["file"] = dict(content["file"])
        return Message(segment)
    if msgtype and msgtype not in MSGTYPE_SEGMENT_MAP:
        # Unknown message types are preserved verbatim so they can be forwarded.
        return Message(MessageSegment.raw(content, event_type="m.room.message"))
    fallback = (
        MessageSegment.text(unescape(body)) if body else MessageSegment.raw(content)
    )
    return Message(fallback)


def _voice_segment_from_content(content: dict[str, Any], body: object) -> MediaSegment:
    voice_file = content.get("org.matrix.msc1767.file")
    voice_file = voice_file if isinstance(voice_file, dict) else {}
    voice_audio = content.get("org.matrix.msc1767.audio")
    voice_audio = voice_audio if isinstance(voice_audio, dict) else {}
    info = content.get("info")
    info = dict(info) if isinstance(info, dict) else {}

    filename = voice_file.get("name")
    if not isinstance(filename, str):
        filename = "Voice message.ogg"
    content_type = voice_file.get("mimetype") or info.get("mimetype") or "audio/ogg"
    duration = voice_audio.get("duration", info.get("duration"))
    waveform = voice_audio.get("waveform")
    if isinstance(waveform, list):
        try:
            waveform = [max(0, min(int(value), 1024)) / 1024 for value in waveform]
        except (TypeError, ValueError, OverflowError):
            waveform = None
    else:
        waveform = None

    uri = content.get("url") or voice_file.get("url") or ""
    segment = MessageSegment.voice(
        uri,
        duration=duration if isinstance(duration, int) else None,
        waveform=waveform,
        filename=filename,
        body=body if isinstance(body, str) else "Voice message",
        content_type=str(content_type),
        info=info,
    )
    encrypted_file = voice_file.get("file") or content.get("file")
    if isinstance(encrypted_file, dict):
        segment.data["file"] = dict(encrypted_file)
    return segment


def is_reply_content(content: dict[str, Any]) -> bool:
    relates_to = content.get("m.relates_to")
    return isinstance(relates_to, dict) and isinstance(
        relates_to.get("m.in_reply_to"), dict
    )


def strip_reply_fallback(body: str) -> str:
    """Remove Matrix's plain-text reply fallback when it is at the start."""
    return _PLAIN_REPLY_FALLBACK_RE.sub("", body, count=1)


def strip_formatted_reply_fallback(body: str) -> str:
    """Remove an Element-style ``<mx-reply>`` fallback at the start."""
    return _REPLY_FALLBACK_RE.sub("", body, count=1)
