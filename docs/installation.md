# 安装与配置

## 安装

### 稳定版

```bash
pip install nonebot-adapter-matrix
```

### 开发版

```bash
pip install git+https://github.com/nonebot/adapter-matrix.git@master
```

### 使用 uv 管理（推荐）

```bash
uv add nonebot-adapter-matrix
```

## 前置条件

Matrix 适配器通过 Client-Server API 与 homeserver 通信，因此 **必须使用 NoneBot 的 ForwardDriver**（如 `httpx`、`aiohttp`）。在 `.env` 中配置：

```ini
DRIVER=~httpx
```

## 三种启动模式

适配器支持三种 Token 管理模式，覆盖不同使用场景。

### 1. 静态 Token（兼容模式）

适合已有 access token 的场景。Token 过期后需手动更新。

```text
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org"
  }
]'
```

### 2. 传统 Matrix 密码登录

提供登录凭据，适配器启动时自动调用 `/login` 获取 Token 对，并在需要时自动刷新。

```text
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

- `login_user` — Matrix 用户 ID，用于 `/login` 请求。
- `login_password` — Matrix 账户密码。
- `login_initial_device_display_name` — 初始设备显示名称（可选）。
- 登录成功后自动设置 `session_type: "legacy_login"`，后续刷新走 `/_matrix/client/v3/refresh`。
- 若 Refresh 返回 `soft_logout: true`，适配器会**自动用密码重新登录**。

### 3. OAuth2 模式

通过 OAuth2 Authorization Code + PKCE 登录，适合支持 Matrix next-gen auth / OIDC 的 homeserver。

最小配置（自动注册 client + 自动打开浏览器）：

```text
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

手动指定已注册的 OAuth2 client：

```text
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

OAuth2 配置字段：

- `oauth_enabled` — 启用 OAuth2 登录流程。
- `oauth_server_url` — OAuth2 / OIDC server 根地址。未提供时会尝试 `/_matrix/client/v1/auth_metadata` 自动发现。
- `oauth_metadata_url` — 直接指定 metadata 文档地址（优先级最高）。
- `oauth_client_id` — 若未提供且 server 暴露 `registration_endpoint`，会自动注册 client。
- `oauth_client_uri` — 动态注册时的 client metadata URI（matrix.org 要求此字段）。
- `oauth_redirect_uri` — 回调地址；省略则使用 loopback + 自动随机端口。
- `oauth_scope` — 自定义 OAuth2 scope（适配器会自动补上设备 scope）。
- `oauth_device_id` — 请求的设备 ID；未提供则自动生成。
- `oauth_open_browser` — 是否自动打开授权页，默认 `false`。
- `oauth_callback_timeout` — 回调超时（秒），默认 `300`。

## 机器人配置字段

`MATRIX_BOTS` 中每个 Bot 均支持以下完整字段：

```text
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

| 字段 | 类型 | 说明 |
|------|------|------|
| `homeserver` | `str` | Matrix homeserver 根地址（必填） |
| `access_token` | `str` | 当前 access token |
| `refresh_token` | `str \| None` | Refresh token，登录后自动获得 |
| `access_token_expires_at_ms` | `int \| None` | access token 绝对过期时间（毫秒） |
| `refresh_before_expiry_ms` | `int` | 提前多久主动刷新，默认 60000 |
| `user_id` | `str \| None` | 校验 token 所属用户 |
| `device_id` | `str \| None` | Matrix 设备 ID |
| `sync_filter` | `str \| dict \| None` | `/sync` 的 filter |
| `set_presence` | `Literal["online","offline","unavailable"]` | 在线状态 |
| `auto_accept_invites` | `bool` | 是否自动接受群聊邀请 |
| `auto_accept_whitelist` | `list[str] \| None` | 白名单，`null` 表示允许所有人 |
| `auto_accept_blacklist` | `list[str]` | 黑名单，优先级高于白名单 |
| `recovery_code` | `str \| None` | MATRIX_RECOVERY_CODE，用于 E2EE 密钥恢复 |
| `e2ee_store_path` | `str \| None` | E2EE 状态持久化目录 |

## 全局配置

```ini
MATRIX_API_TIMEOUT=30.0
MATRIX_SYNC_TIMEOUT=30000
MATRIX_RETRY_INTERVAL=3.0
MATRIX_HANDLE_SELF_MESSAGE=false
MATRIX_HANDLE_OLD_EVENTS=false
MATRIX_PROXY='http://127.0.0.1:7890'
MATRIX_TOKEN_STORE_PATH='.data/matrix-tokens.json'
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `MATRIX_API_TIMEOUT` | `float` | `30.0` | 普通 API 请求超时（秒） |
| `MATRIX_SYNC_TIMEOUT` | `int` | `30000` | `/sync` long-poll 超时（毫秒） |
| `MATRIX_RETRY_INTERVAL` | `float` | `3.0` | 网络错误后重试间隔（秒） |
| `MATRIX_HANDLE_SELF_MESSAGE` | `bool` | `false` | 是否处理机器人自己的消息 |
| `MATRIX_HANDLE_OLD_EVENTS` | `bool` | `false` | 是否处理启动前的旧事件 |
| `MATRIX_PROXY` | `str \| None` | `None` | HTTP 代理地址 |
| `MATRIX_TOKEN_STORE_PATH` | `str \| None` | `None` | Token 持久化文件路径 |

## Token 刷新行为

- 若配置了 `MATRIX_TOKEN_STORE_PATH`，启动时优先从状态文件加载最新 token 和 `session_type`。
- access token 接近过期时会主动刷新（下下次 `/sync` 前）。
- 若 homeserver 返回 `M_UNKNOWN_TOKEN`，适配器尝试用 refresh token 恢复。
- **刷新失败语义**：
  - 网络错误 / 5xx：保留旧 refresh token，稍后重试。
  - 4xx 且 `soft_logout: true`：若有 `login_password`，自动重新登录。
  - 4xx 无 `soft_logout`：视为会话失效，等待重试。
- 刷新成功后会更新内存状态并写回 `MATRIX_TOKEN_STORE_PATH`。
