<p align="center">
  <a href="https://nonebot.dev/"><img src="assets/logo.svg" width="200" height="200" alt="nonebot-adapter-discord"></a>
</p>

<div align="center">

# NoneBot-Adapter-Matrix

_✨ Matrix Client-Server 协议适配 ✨_

</div>

## 安装

```bash
pip install nonebot-adapter-matrix
```
开发版本可从当前仓库构建安装：

```bash
pip install git+https://github.com/elysia-best/adapter-matrix.git@master
```

## 配置

Matrix adapter 使用 Client-Server API 与 homeserver 通信，需要可发起 HTTP 请求的 NoneBot ForwardDriver。

```dotenv
DRIVER=~httpx
```

### MATRIX_BOTS

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org",
    "device_id": "BOTDEVICE",
    "set_presence": "online"
  }
]'
```

字段说明：

- `homeserver`：Matrix homeserver 根地址。
- `access_token`：机器人账号的 Matrix access token。
- `user_id`：可选；启动时会通过 `/account/whoami` 校验 token 所属用户。
- `device_id`：可选；用于记录当前 token 对应设备。
- `sync_filter`：可选；传给 `/sync` 的 filter id 或 filter JSON。
- `set_presence`：可选；`online`、`offline` 或 `unavailable`。

### 其他配置

```dotenv
MATRIX_API_TIMEOUT=30.0
MATRIX_SYNC_TIMEOUT=30000
MATRIX_RETRY_INTERVAL=3.0
MATRIX_HANDLE_SELF_MESSAGE=false
MATRIX_HANDLE_OLD_EVENTS=false
MATRIX_PROXY='http://127.0.0.1:7890'
```

- `MATRIX_API_TIMEOUT`：普通 API 请求超时时间，单位秒。
- `MATRIX_SYNC_TIMEOUT`：`/sync` long-poll 超时时间，单位毫秒。
- `MATRIX_RETRY_INTERVAL`：网络错误后的重试间隔，单位秒。
- `MATRIX_HANDLE_SELF_MESSAGE`：是否处理机器人自己发送的消息。
- `MATRIX_HANDLE_OLD_EVENTS`：是否处理早于本次启动时间的旧事件，默认丢弃旧事件。
- `MATRIX_PROXY`：可选 HTTP 代理。

## 插件示例

```python
from nonebot import on_command
from nonebot.params import CommandArg

from nonebot.adapters.matrix import Bot, Message, MessageEvent, MessageSegment

matcher = on_command("echo")


@matcher.handle()
async def handle_echo(bot: Bot, event: MessageEvent, msg: Message = CommandArg()):
    text = msg.extract_plain_text()
    if text == "mention":
        await matcher.finish(MessageSegment.mention_user(event.get_user_id()))
    if text == "notice":
        await matcher.finish(MessageSegment.notice("这是一条 Matrix notice"))
    await bot.send(event, MessageSegment.text(text or "hello matrix"))
```

### 发送媒体

Matrix 媒体需要先上传到 media repository，消息正文再引用返回的 `mxc://` URI；`MessageSegment.image/file/audio/video` 在传入 bytes 时会自动执行这个流程。

```python
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.matrix import Bot, MessageEvent, MessageSegment

matcher = on_command("image")


@matcher.handle()
async def handle_img_send():
    cur_dir = os.path.dirname(__file__)
    # Read img from current directory
    content = Path(os.path.join(cur_dir, "./assets/test.jpg")).read_bytes()
    await send_img.finish(
        MessageSegment.image(
            content,
            filename="test.jpg",
            content_type="image/jpg"
        )
    )

```

### 常用 Matrix 操作

```python
await bot.react(event.room_id, event.event_id, "👍")
await bot.set_typing_state(event.room_id, typing=True, timeout=5000)
await bot.mark_read(event.room_id, event.event_id)
await bot.redact(event.room_id, event.event_id, reason="handled")
```

## 当前范围

当前实现面向 Matrix Client-Server bot 场景：

- 通过 `/account/whoami` 校验身份。
- 通过 `/sync` long-poll 接收 room timeline、state、typing、receipt 等事件。
- 支持发送 `m.room.message`、上传媒体、reaction、redaction、typing 和 receipt。
- 不包含端到端加密房间支持。
- 不包含 Matrix Application Service API。
- 不持久化 `/sync` 的 `next_batch`；进程内重连会复用内存状态，跨进程重启默认丢弃早于本次启动时间的旧事件。
