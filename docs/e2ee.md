# 端到端加密（E2EE）

适配器支持 Matrix 端到端加密房间，使用上游 `python3-olm` 的 libolm 实现 Olm v1、Megolm v1、SAS 和备份加密，并以 SQLite 保存加密状态。

## 前置条件

安装适配器时会安装 `python3-olm` 和 `cryptography`。运行环境必须能加载 libolm 3.2 或更新版本。

状态格式是新的版本化 SQLite 数据库。旧的 JSON、pickle 或自有状态不会迁移，也不会删除；检测到旧格式、损坏状态或设备身份冲突时，适配器会报错并要求新设备或新目录。

## 启用 E2EE

E2EE 默认启用。需要运行明文设备时，显式设置 `"e2ee_enabled": false`。

```text
MATRIX_BOTS='[
  {
    "homeserver": "https://matrix.example.org",
    "access_token": "YOUR_ACCESS_TOKEN",
    "user_id": "@bot:example.org",
    "device_id": "BOTDEVICE",
    "e2ee_store_path": ".data/e2ee",
    "recovery_key": "EsTb ...",
    "secret_storage_passphrase": "OPTIONAL_PASSPHRASE"
  }
]'
```

`e2ee_store_path` 优先使用；未设置时从 token store 同目录或 `.data/matrix/e2ee/` 派生由 homeserver、用户和设备 ID 绑定的独立目录。目录权限为 `0700`，私密文件权限为 `0600`。

## 配置字段

- `e2ee_store_path` — E2EE 密钥和会话持久化目录。包含 Olm 账户密钥、Megolm 会话、设备密钥缓存等。
- `recovery_key` — Matrix Secret Storage recovery key，用于恢复交叉签名秘密和备份私钥；备份私钥也可通过 `backups().enable(private_key)` 单独导入。
- `secret_storage_passphrase` — 读取现有 Secret Storage 及备份密钥。
- `e2ee_enabled` — 是否初始化 E2EE，默认 `true`。
- `auto_enable_cross_signing`、`auto_enable_backups` — 是否在启动时创建对应状态，默认均为 `false`。
- `backup_download_strategy` — `Manual`、`OneShot` 或 `AfterDecryptionFailure`。
- `encryption_sharing_strategy` — `AllDevices`、`ErrorOnVerifiedUserProblem`、`IdentityBasedStrategy` 或 `OnlyTrustedDevices`。

## 工作原理

### 初始化

Bot 启动时，加密引擎执行以下步骤：

1. **加载或创建 libolm 账户**：生成 Ed25519 签名密钥和 Curve25519 加密密钥对。
2. **恢复持久化会话**：加载已保存的 Olm 会话和 Megolm 入站/出站会话。
3. **上传身份密钥**：将设备密钥（带自签名）上传到 homeserver。
4. **补充一次性密钥（OTK）**：确保每个 device 至少有一定数量的 signed_curve25519 OTK。
5. **上传 fallback 密钥**：当 OTK 耗尽时使用。
6. **密钥备份恢复**：只有调用恢复操作或配置恢复凭据时才执行，不会把恢复密钥发送给插件事件。

### 进入加密房间

当 `/sync` 收到 `m.room.encryption` 状态事件时，适配器自动标记该房间为加密房间。

### 发送加密消息

向加密房间发送消息时：

1. 适配器检测房间的加密状态。
2. 如果是**首次**使用当前出站 Megolm 会话，先通过 Olm to-device 消息向房间内所有成员设备共享 Megolm 会话密钥。
3. 将明文消息用 Megolm 加密，以 `m.room.encrypted` 事件发送。

### 接收加密消息

收到 `m.room.encrypted` 事件时：

1. 检查本地是否已有对应的入站 Megolm 会话。
2. 如果有，解密得到明文，分派给插件处理。
3. 如果没有（可能还没收到 key），持久化待解密事件并发出密钥请求；密钥到达后自动重试。

### 设备列表跟踪

每个同步周期，适配器处理 `device_lists` 变更：

- `changed` 列表中的用户：重新查询其设备密钥缓存，确保密钥共享准确。
- `left` 列表中的用户：清理本地设备密钥缓存。

### To-Device 事件

适配器处理以下 to-device 事件类型：

| 事件类型 | 说明 |
|----------|------|
| `m.room.encrypted` (Olm) | 解密后递归处理内部包裹的 `m.room_key` 等事件 |
| `m.room_key` | 导入 Megolm 入站会话密钥 |
| `m.forwarded_room_key` | 转发的 Megolm 会话密钥 |
| `m.room_key_request` | 密钥请求（只向自己的已验证设备转发） |
| `m.room_key.withheld` | 密钥被拒通知 |

## 密钥恢复

当配置了 `recovery_key`（`recovery_code` 为兼容别名）或显式调用恢复操作时：

1. 启动时加密引擎调用 `/room_keys/version` 获取最新备份版本。
2. 遍历该版本下的所有 `room_id → session_id → session_data`。
3. 使用 recovery code（Curve25519 私钥）解密备份的 Megolm 会话密钥。
4. 将恢复的会话导入本地 Megolm 管理器，使其可立即解密历史消息。

这使得新设备（或不持久化 Megolm 会话的设备）在无需其他设备在线的情况下，也能解密加密房间的消息。

## 注意事项

- **持久化**：E2EE 状态（Olm 账户、Megolm 会话、设备密钥缓存、验证和备份进度）会持久化到绑定身份的 SQLite 数据库。
- **密钥验证**：`matrix_auto_accept_verification` 只自动接受请求，不会自动确认 SAS 数字、emoji 或二维码；插件必须调用 `confirm()`。
- **公开对象**：使用 `bot.encryption()`、`bot.get_room(room_id)`、`Device`、`UserIdentity`、`VerificationRequest`、`SasVerification` 和 `QrVerification` 管理状态。
- **附件**：加密房间中的 bytes 媒体上传前使用 Matrix v2 附件格式加密，接收端通过 `download_encrypted_file()` 校验并解密。
- **首次消息延迟**：进入新加密房间发送首条消息时，需要先向所有成员设备共享 Megolm 密钥，可能有一定延迟。
