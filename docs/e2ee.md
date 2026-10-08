# 端到端加密（E2EE）

适配器支持 Matrix 端到端加密房间，使用纯 Python 加密状态机处理设备密钥、to-device 消息和房间会话。

## 前置条件

E2EE 依赖 `cryptography`，不需要安装 libolm 或其他平台相关的 Olm 扩展。

当前后端提供架构无关的纯 Python 状态机和认证加密记录，适合在适配器内持久化和测试；它不读取旧 libolm pickle。与 Element 或 matrix-rust-sdk 的完整标准 Olm/Megolm ratchet 互操作仍需要专门的协议向量验证，不能仅凭同名的 Matrix 算法字段推断兼容。

## 启用 E2EE

E2EE 是 **opt-in** 的：只有配置了 `e2ee_store_path` 或 `MATRIX_TOKEN_STORE_PATH` 时才会启用。

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

如果只配置了 `MATRIX_TOKEN_STORE_PATH`，存储路径会从该路径的同级目录自动派生（如 `e2ee_<hash>_<user>`）。两个都未设置则 E2EE 完全禁用。

## 配置字段

- `e2ee_store_path` — E2EE 密钥和会话持久化目录。包含 Olm 账户密钥、Megolm 会话、设备密钥缓存等。
- `recovery_key` — Matrix recovery key，从服务端密钥备份恢复 Megolm 会话密钥。
- `secret_storage_passphrase` — 读取现有 Secret Storage 及备份密钥。

## 工作原理

### 初始化

Bot 启动时，加密引擎执行以下步骤：

1. **加载或创建 Olm 账户**：生成 Ed25519 签名密钥和 Curve25519 加密密钥对。
2. **恢复持久化会话**：加载已保存的 Olm 会话和 Megolm 入站/出站会话。
3. **上传身份密钥**：将设备密钥（带自签名）上传到 homeserver。
4. **补充一次性密钥（OTK）**：确保每个 device 至少有一定数量的 signed_curve25519 OTK。
5. **上传 fallback 密钥**：当 OTK 耗尽时使用。
6. **密钥备份恢复**：如果配置了 `recovery_code`，从服务端备份拉取并解密所有可恢复的 Megolm 会话密钥。

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
3. 如果没有（可能还没收到 key），跳过该事件并记录 trace 日志。

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
| `m.room_key_request` | 密钥请求（记录但不自动重发） |
| `m.room_key.withheld` | 密钥被拒通知 |

## 密钥恢复

当配置了 `recovery_key`（`recovery_code` 为兼容别名）或 `secret_storage_passphrase` 时：

1. 启动时加密引擎调用 `/room_keys/version` 获取最新备份版本。
2. 遍历该版本下的所有 `room_id → session_id → session_data`。
3. 使用 recovery code（Curve25519 私钥）解密备份的 Megolm 会话密钥。
4. 将恢复的会话导入本地 Megolm 管理器，使其可立即解密历史消息。

这使得新设备（或不持久化 Megolm 会话的设备）在无需其他设备在线的情况下，也能解密加密房间的消息。

## 注意事项

- **持久化**：E2EE 状态（Olm 账户、Megolm 会话、设备密钥缓存）会持久化到 `e2ee_store_path`。如果未设置且未提供 `MATRIX_TOKEN_STORE_PATH`，加密引擎完全不会初始化。
- **存储格式**：加密状态以版本化 JSON 保存。旧的 `python3-olm` pickle 不会被加载，升级后会创建新的设备密钥状态。
- **密钥验证**：默认关闭自动 SAS。设置全局 `MATRIX_AUTO_ACCEPT_VERIFICATION=true` 后才会自动接受协议支持的 SAS 请求；无效流程不会被标记为成功。
- **首次消息延迟**：进入新加密房间发送首条消息时，需要先向所有成员设备共享 Megolm 密钥，可能有一定延迟。
