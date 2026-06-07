# 介绍

NoneBot Adapter Matrix 是 [NoneBot2](https://nonebot.dev/) 的 Matrix 协议适配器。它实现了 [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/) 的核心子集，让开发者可以用 Python 编写能加入 Matrix 聊天室、收发消息、响应指令的机器人。

## 设计理念

适配器完全遵循 NoneBot 的事件驱动模型：通过 `/sync` 长轮询持续拉取 Matrix homeserver 的事件流，将房间消息、邀请、成员变更等事件转换为标准的 NoneBot `Event`，分派给对应的[事件处理器](https://nonebot.dev/docs/advanced/builtin-conventions)。开发者只需像处理普通聊天消息一样编写插件，无需关心底层 Matrix 协议的细节。

## 核心能力

- **消息收发**：支持 `m.room.message` 纯文本、HTML、通知、表情消息，以及图片/文件/音频/视频等媒体上传与发送。
- **房间操作**：加入房间、自动接受邀请（支持白名单/黑名单）、查询成员列表。
- **交互增强**：发送表情反应、撤回消息、设置输入状态、标记已读。
- **三种认证模式**：静态 Token、传统 Matrix 密码登录、OAuth2 Authorization Code + PKCE。
- **端到端加密**（opt-in）：基于 Olm/Megolm 协议的 E2EE 支持，可收发加密房间的消息，并支持通过 `MATRIX_RECOVERY_CODE` 从服务端密钥备份恢复会话。

## 架构概览

适配器由以下核心模块组成：

- `Bot`：机器人实例，封装 API 调用与加密引擎。
- `Adapter`：适配器主类，负责启动、同步循环、Token 生命周期管理。
- `Event`：Matrix 事件映射，包括消息、成员变动、反应、输入状态等。
- `Message` / `MessageSegment`：消息与消息段的构造与解析。
- `CryptoEngine`：E2EE 加密引擎，编排 Olm/Megolm 会话。

运行时每个 `Bot` 实例对应一个 Matrix 用户；Adapter 为每份 `BotInfo` 配置启动一个独立的同步循环。

## 相关链接

- [NoneBot2 官方文档](https://nonebot.dev/)
- [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/)
- [MSC2967: OAuth2 Authorization Code](https://github.com/matrix-org/matrix-spec-proposals/pull/2967)
- [Olm / Megolm 协议](https://matrix.org/docs/guides/end-to-end-encryption-implementation-guide)
