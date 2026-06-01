from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from typing_extensions import Self, override

from nonebot.adapters import (
    Message as BaseMessage,
    MessageSegment as BaseMessageSegment,
)

from nonebot.compat import type_validate_python

from .api.model import UploadResponse
from .api.types import EventId, MxcUri, UserId
from .utils import escape, unescape

MATRIX_HTML_FORMAT = "org.matrix.custom.html"
MEDIA_SEGMENT_TYPES = {"image", "file", "audio", "video"}
SEGMENT_MSGTYPE_MAP = {
    "text": "m.text",
    "notice": "m.notice",
    "emote": "m.emote",
    "image": "m.image",
    "file": "m.file",
    "audio": "m.audio",
    "video": "m.video",
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
    def raw(content: dict[str, Any], event_type: str | None = None) -> RawSegment:
        return RawSegment("raw", {"content": content, "event_type": event_type})

    @override
    def is_text(self) -> bool:
        return self.type in {"text", "notice", "emote", "html", "mention_user"}

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
    "reply": ReplySegment,
    "image": MediaSegment,
    "file": MediaSegment,
    "audio": MediaSegment,
    "video": MediaSegment,
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
    return MediaSegment(
        segment_type,
        {
            "content": raw_content,
            "url": url,
            "filename": file_name,
            "body": body or file_name or segment_type,
            "content_type": content_type,
            "info": info or {},
        },
    )


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


async def build_message_content(
    message: str | BaseMessage | BaseMessageSegment,
    *,
    bot: MediaUploader | None = None,
) -> dict[str, Any]:
    return await _build_message_content_async(_ensure_message(message), bot=bot)


def _build_message_content(message: Message) -> dict[str, Any]:
    content, _ = _build_message_payload(message)
    return content


async def _build_message_content_async(
    message: Message, *, bot: MediaUploader | None
) -> dict[str, Any]:
    content, media_segment = _build_message_payload(message)
    if media_segment is None:
        return content
    return await _materialize_media_content(content, media_segment, bot=bot)


def _build_message_payload(  # noqa: C901
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
        elif segment.type == "reply":
            reply_to = str(segment.data["event_id"])
        elif segment.type in MEDIA_SEGMENT_TYPES:
            media_segment = segment
        elif segment.type == "raw":
            return dict(segment.data["content"]), None

    if media_segment is not None:
        content = {
            "msgtype": SEGMENT_MSGTYPE_MAP[media_segment.type],
            "body": media_segment.data.get("body")
            or media_segment.data.get("filename")
            or media_segment.type,
        }
        if media_segment.data.get("url") is not None:
            content["url"] = media_segment.data["url"]
        if media_segment.data.get("info"):
            content["info"] = media_segment.data["info"]
        _apply_relations(content, reply_to=reply_to, mentions=mentions)
        return content, media_segment

    body = "".join(body_parts)
    content: dict[str, Any] = {"msgtype": message_type or "m.text", "body": body}
    if has_html or has_mention:
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = "".join(formatted_parts)
    _apply_relations(content, reply_to=reply_to, mentions=mentions)
    return content, None


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
) -> None:
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    if mentions:
        content["m.mentions"] = {"user_ids": sorted(mentions)}


def message_from_content(content: dict[str, Any]) -> Message:
    msgtype = content.get("msgtype")
    body = content.get("body", "")
    if msgtype in {"m.text", "m.notice", "m.emote"}:
        if content.get("format") == MATRIX_HTML_FORMAT and content.get(
            "formatted_body"
        ):
            return Message(MessageSegment.html(body, content["formatted_body"]))
        segment_type = MSGTYPE_SEGMENT_MAP.get(msgtype, "text")
        factory = getattr(MessageSegment, segment_type)
        return Message(factory(body))
    if msgtype in {"m.image", "m.file", "m.audio", "m.video"}:
        segment_type = MSGTYPE_SEGMENT_MAP[msgtype]
        factory = getattr(MessageSegment, segment_type)
        return Message(
            factory(
                content.get("url", ""),
                body=body,
                info=content.get("info"),
            )
        )
    if body:
        return Message(MessageSegment.text(unescape(body)))
    return Message(MessageSegment.raw(content))
