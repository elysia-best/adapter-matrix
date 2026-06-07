# 更新日志

## v0.3.0

- ✨ 新增 OAuth2 Authorization Code + PKCE 登录支持，兼容 Matrix next-gen auth / OIDC。
- ✨ 新增端到端加密（E2EE）支持，可收发加密房间消息。
- ✨ 新增 `MATRIX_RECOVERY_CODE` 密钥恢复，支持从服务端密钥备份恢复 Megolm 会话。
- ✨ 新增 `auto_accept_invites` / `auto_accept_whitelist` / `auto_accept_blacklist` 邀请自动接受。
- ✨ 新增 `MATRIX_TOKEN_STORE_PATH` Token 持久化。
- 🐛 修复加密房间的回复和提及功能。
- 🐛 优化 API 轮询中止条件。

## v0.2.x

- 基于 `m.mentions` 和 `body` 关键词实现 `to_me` 检测。
- 支持消息反应、撤回、输入状态、已读回执。
- 支持媒体上传与 `mxc://` URI 引用。
- 支持传统 Matrix 密码登录和 Token 刷新。
- 支持 `/sync` filter 和 presence 设置。
