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

### 三种启动模式

适配器支持三种 token 管理模式：

#### 1. 静态 token 模式（兼容模式）

适合已有 access token 的场景。不会自动获取或刷新 token。

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org"
  }
]'
```

注意：此模式下如果没有额外提供登录凭据或 OAuth2 配置，协议上无法自动获取首个 refresh token。access token 过期后需要手动更新配置。

#### 2. 传统 Matrix 登录模式

提供登录凭据，适配器启动时自动调用 `/login` 获取 token 对，并在需要时自动 refresh。

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "",
    "login_user": "@bot:example.org",
    "login_password": "your-password",
    "device_id": "BOTDEVICE",
    "set_presence": "online"
  }
]'
```

- `login_user`：Matrix 用户 ID，用于 `/login` 请求。
- `login_password`：Matrix 账户密码。
- `login_initial_device_display_name`：可选；初始设备显示名称。
- 登录成功后会自动设置 `session_type: "legacy_login"`，后续 refresh 走 `/_matrix/client/v3/refresh`。
- 若 refresh 返回 `soft_logout: true`，适配器会自动用密码重新登录。

#### 3. OAuth2 模式

通过 OAuth2 Authorization Code + PKCE 登录，适合支持 Matrix next-gen auth / OIDC 的 homeserver。

最小配置：

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "",
    "oauth_enabled": true,
    "oauth_server_url": "https://account.matrix.org",
    "oauth_open_browser": true
  }
]'
```

也可以手动指定已注册的 client：

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "",
    "oauth_enabled": true,
    "oauth_server_url": "https://account.matrix.org",
    "oauth_client_id": "your-client-id",
    "oauth_redirect_uri": "https://your-app.example/callback"
  }
]'
```

- `oauth_enabled`：启用 OAuth2 登录流程。
- `oauth_server_url`：可选；直接指定 OAuth2/OIDC server 根地址，例如 `https://account.matrix.org`。若未提供，会优先尝试 Matrix `/_matrix/client/v1/auth_metadata` 自动发现。
- `oauth_metadata_url`：可选；直接指定 metadata 文档地址，优先级高于 `oauth_server_url`。
- `oauth_client_id`：可选；若未提供且 server 暴露 `registration_endpoint`，适配器会自动注册一个 OAuth2 client 并持久化保存。
- `oauth_client_uri`：可选；动态注册时写入 client metadata。某些 Matrix Authentication Service（例如 matrix.org）要求该字段存在；未配置时默认使用 `homeserver` 作为回退值。需要注意这里需要填写有效的域名
- `oauth_redirect_uri`：可选；若不提供，则默认使用 loopback 回调并自动选择一个可用随机端口。若提供 localhost / `127.0.0.1` 回调地址，则必须显式写出端口，且注册、授权、换 token 全程使用该 URI 原样值；若想用自动随机端口，请直接省略 `oauth_redirect_uri`。若提供外部回调地址，则需要手动复制授权结果中的 `code` 或完整回调 URL 到终端。
- `oauth_scope`：可选；默认会构造符合 MSC2967 的 scope，并自动补上 `urn:matrix:org.matrix.msc2967.client:device:{DEVICE_ID}`；若你自定义 scope，适配器仍会补 device scope。
- `oauth_device_id`：可选；指定请求的 Matrix device ID。未提供时会自动生成一个 12 位的大写字母数字 ID。
- `oauth_open_browser`：是否自动打开浏览器访问授权 URL，默认 `false`。
- `oauth_callback_timeout`：等待回调的超时时间（秒），默认 `300`。

OAuth2 登录流程：
1. 适配器按如下顺序发现元数据：`oauth_metadata_url` → `/_matrix/client/v1/auth_metadata`（及其 unstable MSC2965 端点）→ `oauth_server_url` 兜底。
2. 校验 server 支持 `response_type=code`、`response_mode=fragment` 和 PKCE `S256`。
3. 若未配置 `oauth_client_id`，则通过 metadata 返回的 `registration_endpoint` 自动注册 client。
4. 生成 device ID、`state`、`code_verifier`/`code_challenge`，并构造符合 MSC2967 的 scope。
5. 输出授权 URL（若 `oauth_open_browser: true` 则自动打开浏览器）。
6. 获取 authorization code 并交换首个 access/refresh token。
7. 后续 refresh 走 metadata 返回的 OAuth2 token endpoint（`grant_type=refresh_token`）。

### 通用字段

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "refresh_token": "OPTIONAL_REFRESH_TOKEN",
    "access_token_expires_at_ms": 1760000000000,
    "refresh_before_expiry_ms": 60000,
    "user_id": "@bot:example.org",
    "device_id": "BOTDEVICE",
    "sync_filter": {"room": {"timeline": {"limit": 50}}},
    "set_presence": "online"
  }
]'
```

- `homeserver`：Matrix homeserver 根地址（必填）。
- `access_token`：当前使用的 Matrix access token（使用登录模式时可留空）。
- `refresh_token`：通常由登录流程获得，不要求手动填写。持久化到 `MATRIX_TOKEN_STORE_PATH` 后会自动加载。
- `access_token_expires_at_ms`：当前 access token 的绝对过期时间戳，单位毫秒。
- `refresh_before_expiry_ms`：距离过期多久前主动 refresh，默认 `60000`（1 分钟）。
- `user_id`：启动时通过 `/account/whoami` 校验 token 所属用户。
- `device_id`：记录当前 token 对应的 Matrix 设备 ID。
- `sync_filter`：传给 `/sync` 的 filter id 或 filter JSON。
- `set_presence`：`online`、`offline` 或 `unavailable`。

### 其他配置

```dotenv
MATRIX_API_TIMEOUT=30.0
MATRIX_SYNC_TIMEOUT=30000
MATRIX_RETRY_INTERVAL=3.0
MATRIX_HANDLE_SELF_MESSAGE=false
MATRIX_HANDLE_OLD_EVENTS=false
MATRIX_PROXY='http://127.0.0.1:7890'
MATRIX_TOKEN_STORE_PATH='.data/matrix-tokens.json'
```

- `MATRIX_API_TIMEOUT`：普通 API 请求超时时间，单位秒。
- `MATRIX_SYNC_TIMEOUT`：`/sync` long-poll 超时时间，单位毫秒。
- `MATRIX_RETRY_INTERVAL`：网络错误后的重试间隔，单位秒。
- `MATRIX_HANDLE_SELF_MESSAGE`：是否处理机器人自己发送的消息。
- `MATRIX_HANDLE_OLD_EVENTS`：是否处理早于本次启动时间的旧事件，默认丢弃旧事件。
- `MATRIX_PROXY`：可选 HTTP 代理。
- `MATRIX_TOKEN_STORE_PATH`：可选 token 状态文件路径；配置后会将 token 持久化到该文件，重启时自动加载。

### Refresh Token 行为

- 启动时若配置了 `MATRIX_TOKEN_STORE_PATH`，会优先读取状态文件中的最新 token 和 `session_type`。
- access token 接近 `access_token_expires_at_ms` 时，会在下一次 `/sync` 前主动 refresh。
- 若 homeserver 提前使当前 token 失效并返回 `M_UNKNOWN_TOKEN`，adapter 会尝试使用 refresh token 恢复。
- **refresh 失败语义**：
  - 网络错误或 5xx：保留旧 refresh token，稍后重试。
  - 4xx 且带 `soft_logout: true`：若配置了 `login_password`，自动重新登录。
  - 4xx 无 `soft_logout`：视为会话失效，等待重试。
- refresh 成功后更新内存中的 `access_token`、`refresh_token`、`access_token_expires_at_ms`，并写回状态文件。
- adapter 不会回写 `.env` 或其他部署配置；`/sync` 的 `next_batch` 仍然不会持久化。

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
