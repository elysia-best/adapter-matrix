# 使用样例

本页提供从入门到进阶的完整插件示例，覆盖消息收发、媒体发送、房间操作、事件处理等场景。

## 基础 echo 机器人

```python
from nonebot import on_command
from nonebot.params import CommandArg

from nonebot.adapters.matrix import Bot, Message, MessageEvent, MessageSegment

matcher = on_command("echo")


@matcher.handle()
async def handle_echo(bot: Bot, event: MessageEvent, msg: Message = CommandArg()):
    text = msg.extract_plain_text().strip()
    if text == "mention":
        await matcher.finish(MessageSegment.mention_user(event.get_user_id()))
    if text == "notice":
        await matcher.finish(MessageSegment.notice("这是一条 Matrix notice"))
    await bot.send(event, MessageSegment.text(text or "hello matrix"))
```

要点：

- 使用 `on_command` 注册命令；用 `CommandArg` 提取命令参数。
- 通过 `bot.send(event, message)` 向事件来源房间发送消息。
- `MessageSegment.text/content/notice/emote` 提供丰富的消息类型。

## 发送媒体

Matrix 媒体需要先上传到 media repository，再由消息正文引用 `mxc://` URI。`MessageSegment.image/file/audio/video` 在传入 `bytes` 时**自动执行上传流程**，无需手动处理。

```python
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.matrix import Bot, MessageEvent, MessageSegment

matcher = on_command("image")


@matcher.handle()
async def handle_send_image(bot: Bot, event: MessageEvent):
    img_bytes = Path("./assets/photo.jpg").read_bytes()
    await bot.send(
        event,
        MessageSegment.image(
            img_bytes,
            filename="photo.jpg",
            content_type="image/jpeg",
        ),
    )
```

> **注意**：`content_type` 必须传入正确的 MIME Type（如 `image/jpeg`），否则客户端可能无法正常显示。

也可以直接传入已有的 `mxc://` URI（不会触发上传）：

```python
MessageSegment.image("mxc://example.org/AbCdEf1234", filename="pic.jpg")
```

## 发送 HTML 富文本

```python
matcher = on_command("html")


@matcher.handle()
async def handle_html(bot: Bot, event: MessageEvent):
    await bot.send(
        event,
        MessageSegment.html(
            "这是一条带格式的消息",
            "这是一条<b>带格式</b>的<em>消息</em>",
        ),
    )
```

## @ 提及用户

```python
matcher = on_command("call")


@matcher.handle()
async def handle_mention(bot: Bot, event: MessageEvent):
    await bot.send(
        event,
        MessageSegment.text("你好，")
        + MessageSegment.mention_user("@alice:example.org", "Alice")
        + MessageSegment.text("，请看这个消息"),
    )
```

## 消息反应

```python
matcher = on_command("approve")


@matcher.handle()
async def handle_react(bot: Bot, event: MessageEvent):
    await bot.react(event.room_id, event.event_id, "👍")
```

## 撤回消息

```python
matcher = on_command("delete")


@matcher.handle()
async def handle_redact(bot: Bot, event: MessageEvent):
    await bot.redact(event.room_id, event.event_id, reason="用户请求撤回")
```

## 输入状态

```python
matcher = on_command("typing")


@matcher.handle()
async def handle_typing(bot: Bot, event: MessageEvent):
    # 显示"正在输入……"，5 秒后自动清除
    await bot.set_typing_state(event.room_id, typing=True, timeout=5000)
    await bot.send(event, MessageSegment.text("这是模拟输入的效果"))
```

## 标记已读

```python
@matcher.handle()
async def mark_as_read(bot: Bot, event: MessageEvent):
    await bot.mark_read(event.room_id, event.event_id)
```

## 加入房间

```python
matcher = on_command("join")


@matcher.handle()
async def handle_join(bot: Bot, event: MessageEvent, msg: Message = CommandArg()):
    room_alias = msg.extract_plain_text().strip()
    if room_alias:
        await bot.join_room(room_id=room_alias, reason="通过命令加入")
```

## 处理房间邀请

适配器支持按白名单/黑名单自动接受邀请。收到邀请时触发 `InviteEvent`，你也可以编写插件手动处理：

```python
from nonebot import on_type
from nonebot.adapters.matrix import Bot, InviteEvent

on_invite = on_type(InviteEvent, block=False)


@on_invite.handle()
async def handle_invite(bot: Bot, event: InviteEvent):
    if event.room_id:
        await bot.join_room(room_id=event.room_id, reason="自动加入")
```

## 构造原始消息

某些场景下需要直接发送符合 Matrix 规范的 JSON 消息体：

```python
matcher = on_command("raw")


@matcher.handle()
async def handle_raw(bot: Bot, event: MessageEvent):
    await bot.send(
        event,
        MessageSegment.raw(
            {
                "msgtype": "m.text",
                "body": "这条消息通过原始格式发送",
            },
            event_type="m.room.message",
        ),
    )
```

## 在加密房间工作

如果启用了 E2EE，适配器会自动对加密房间的消息进行加密/解密。插件代码与明文模式**完全一致**，无需任何特殊处理：

```python
matcher = on_command("secret")


@matcher.handle()
async def handle_secret(bot: Bot, event: MessageEvent):
    # 在加密房间中，此消息会自动用 Megolm 加密后发送
    await bot.send(event, MessageSegment.text("这是一条加密消息"))
    # 同理，收到的加密消息也会自动解密为明文后传入 event
```

## 处理设备验证

验证请求和状态会以带有 `handle` 的类型化事件分派。自动接受配置只会调用
`VerificationRequest.accept()`，不会替用户确认 SAS 或二维码；确认动作应由插件在
展示比较结果并获得用户明确操作后调用。

```python
from nonebot import on_command, on_type
from nonebot.adapters.matrix import (
    Bot,
    QrVerification,
    SasVerification,
    VerificationEvent,
    VerificationRequest,
)

verification_events = on_type(VerificationEvent, block=False)
confirm = on_command("verify-confirm")
pending: dict[tuple[str, str], SasVerification | QrVerification] = {}


@verification_events.handle()
async def show_verification(event: VerificationEvent):
    handle = event.handle
    if isinstance(handle, VerificationRequest):
        if handle.is_ready():
            # 交给插件 UI 展示可用方式；这里仅启动 SAS，不自动确认。
            sas = await handle.start_sas()
            if sas is not None:
                pending[handle.other_user_id, handle.flow_id] = sas
                print(sas.decimals(), sas.emoji())
    elif isinstance(handle, SasVerification) and handle.can_be_presented():
        pending[handle.request.other_user_id, handle.request.flow_id] = handle
        print(handle.decimals(), handle.emoji())


@confirm.handle()
async def confirm_verification(bot: Bot, event):
    # 生产插件应把确认动作接到自己的按钮、Web UI 或命令权限上。
    for verification in pending.values():
        if isinstance(verification, SasVerification):
            await verification.confirm()
            break
```

二维码由插件负责展示或扫码，适配器只提供标准 Matrix 字节编码：

```python
qr = await request.generate_qr_code()
if qr is not None:
    image_bytes = qr.to_bytes()  # 交给二维码 UI 展示

# 扫描后的原始二维码字节交回对应请求；不会自动确认信任。
qr = await request.scan_qr_code(scanned_bytes)
if qr is not None:
    await qr.confirm()
```

## 组合消息段

`Message` 支持 `+` 运算符拼接多个 `MessageSegment`：

```python
msg = (
    MessageSegment.notice("系统提示：")
    + MessageSegment.mention_user(user_id="alice@example.org", "Alice")
    + MessageSegment.text(" 完成了任务 ")
    + MessageSegment.emote("💡 太棒了")
)
await bot.send(event, msg)
```
