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
    "set_presence": "online",
    "auto_accept_invites": true,
    "auto_accept_whitelist": null,
    "auto_accept_blacklist": []
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
- `auto_accept_invites`：是否启用自动接受群聊邀请，默认 `false`。
- `auto_accept_whitelist`：允许自动接受邀请的用户白名单，值为用户 ID 列表（如 `["@alice:example.org"]`）。设为 `null` 表示允许所有用户，设为空列表 `[]` 表示不允许任何用户（行为同 `auto_accept_invites: false`）。
- `auto_accept_blacklist`：禁止自动接受邀请的用户黑名单，默认 `[]`。黑名单优先级高于白名单。

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
- refresh 成功后更新内存中的 `access_token`、`refresh_token`、`access_token_expires_at_ms`，并写回状态 json 文件。
- 注意，adapter 不会回写获取的 Token 到 `.env` 或其他 dotenv 文件；同样，`/sync` 的 `next_batch` 不会持久化。

### 端到端加密 (E2EE)

适配器支持 Matrix 端到端加密房间，使用 Olm/Megolm 协议进行消息加解密。E2EE 是 opt-in 的：只有配置了 `e2ee_store_path` 或 `MATRIX_TOKEN_STORE_PATH` 时才会启用。

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org",
    "device_id": "BOTDEVICE",
    "recovery_code": "EsTb ...",
    "e2ee_store_path": ".data/e2ee_store"
  }
]'
```

- `recovery_code`：**MATRIX_RECOVERY_CODE**，用于从服务端密钥备份恢复 Megolm 会话密钥。格式为 base58 编码的 Curve25519 私钥，可以在 Element 等客户端的「安全与隐私」设置中找到。配置后，启动时自动从服务端备份恢复所有可解密的会话密钥，无需其他设备在线。
- `e2ee_store_path`：E2EE 加密状态的持久化目录路径。为 `None`（默认）时，会从 `MATRIX_TOKEN_STORE_PATH` 同目录下自动派生一个子目录（如 `e2ee_<hash>_<user>`）。如果两者均未设置，E2EE 完全禁用。

**E2EE 工作流程：**

1. **启动时**：生成 Olm 设备密钥（Ed25519 + Curve25519），上传到 homeserver，确保一次性密钥 (OTK) 数量充足，上传 fallback 密钥作为备用。若配置了 `recovery_code`，从服务端密钥备份恢复 Megolm 会话。
2. **进入加密房间**：当同步到 `m.room.encryption` 状态事件时，自动标记该房间为加密。首次向加密房间发送消息前，会通过 Olm to-device 消息向所有成员设备共享 Megolm 会话密钥。
3. **收发加密消息**：发出的消息自动用 Megolm 加密后以 `m.room.encrypted` 事件发送；收到的加密消息自动解密为明文后再分派给插件处理。
4. **设备列表跟踪**：自动处理 `/sync` 中的 `device_lists` 变更，查询新设备的密钥，保持设备密钥缓存更新。

**注意事项：**
- E2EE 依赖 `python-olm` 库（libolm 的 Python 绑定）。libolm 的 Olm session `from_pickle` 在某些平台/Python 版本上可能不可用，此时 Olm 会话不会跨重启持久化（不影响正常使用，仅影响重启后首次密钥共享的效率）。
- 机器人目前不处理密钥验证（SAS/emoji），其他用户的设备标记为「未验证」不影响正常收发消息。

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

需要注意，`content_type` 字段需要传入**正确的 MIME Type** 才能在客户端显示！

### 常用 Matrix 操作

```python
await bot.react(event.room_id, event.event_id, "👍")
await bot.set_typing_state(event.room_id, typing=True, timeout=5000)
await bot.mark_read(event.room_id, event.event_id)
await bot.redact(event.room_id, event.event_id, reason="handled")
await bot.join_room("!roomid:example.org", reason="Auto-joined")
```

### 自动接受邀请

适配器支持按照白名单 / 黑名单自动接受 Matrix 群聊邀请。当收到邀请时，适配器会从 `invite_state` 中提取邀请人信息，根据配置决定是否自动加入房间。

**配置示例：**

```dotenv
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org",
    "auto_accept_invites": true,
    "auto_accept_whitelist": ["@alice:example.org", "@bob:example.org"],
    "auto_accept_blacklist": ["@spammer:matrix.org"]
  }
]'
```

- 若 `auto_accept_whitelist` 设置为 `null`（或不配置），则允许任意用户邀请机器人入群。
- 黑名单优先级高于白名单：即使用户在白名单中，若同时出现在黑名单中也不会自动接受邀请。
- 自动加入失败时仅记录错误日志，不会中断同步循环。

## 当前实现范围

当前实现面向 Matrix Client-Server bot 场景：

- 通过 `/account/whoami` 校验身份。
- 通过 `/sync` long-poll 接收 room timeline、state、typing、receipt 等事件。
- 支持发送 `m.room.message`、上传媒体、reaction、redaction、typing、receipt 和 join room。
- 支持端到端加密房间 (E2EE)，可收发加密消息，支持通过 MATRIX_RECOVERY_CODE 从服务端密钥备份恢复会话。
- 不包含 Matrix Application Service API。
- 不持久化 `/sync` 的 `next_batch`；进程内重连会复用内存状态，跨进程重启默认丢弃早于本次启动时间的旧事件。
