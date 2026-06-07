配置（Config）
===============

Matrix 适配器使用 Pydantic 模型管理配置，支持从 ``.env`` 文件和环境变量自动加载。

.. automodule:: nonebot.adapters.matrix.config
   :members:

配置模型
--------

:class:`Config`
   - ``matrix_bots`` — 机器人配置列表（对应 ``MATRIX_BOTS`` 环境变量）。
   - ``matrix_api_timeout`` — 普通 API 超时（秒），默认 ``30.0``。
   - ``matrix_sync_timeout`` — ``/sync`` long-poll 超时（毫秒），默认 ``30000``。
   - ``matrix_retry_interval`` — 网络错误重试间隔（秒），默认 ``3.0``。
   - ``matrix_handle_self_message`` — 是否处理自己的消息，默认 ``false``。
   - ``matrix_handle_old_events`` — 是否处理启动前的旧事件，默认 ``false``。
   - ``matrix_proxy`` — HTTP 代理地址，默认 ``None``。
   - ``matrix_token_store_path`` — Token 持久化文件路径，默认 ``None``。

:class:`BotInfo`
   每个机器人的独立配置项，对应 ``MATRIX_BOTS`` 数组中的每个对象。

   **连接配置**
     * ``homeserver`` — Matrix homeserver 根地址（必填）。
     * ``access_token`` — 当前 access token。
     * ``refresh_token`` — Refresh token（登录后获得）。
     * ``access_token_expires_at_ms`` — Token 过期时间（毫秒时间戳）。
     * ``refresh_before_expiry_ms`` — 提前刷新阈值（毫秒），默认 ``60000``。
     * ``user_id`` — 校验 token 所属用户。
     * ``device_id`` — 设备标识符。
     * ``sync_filter`` — ``/sync`` filter，可为 filter ID 字符串或 filter JSON。
     * ``set_presence`` — 在线状态：``"online"`` / ``"offline"`` / ``"unavailable"``。

   **传统登录凭据**
     * ``login_user`` — 登录用户名。
     * ``login_password`` — 登录密码。
     * ``login_initial_device_display_name`` — 初始设备显示名。

   **OAuth2 配置**
     * ``oauth_enabled`` — 是否启用 OAuth2 流程。
     * ``oauth_server_url`` — OAuth2/OIDC server 根地址。
     * ``oauth_metadata_url`` — Metadata 文档地址。
     * ``oauth_client_id`` — OAuth2 client ID。
     * ``oauth_client_uri`` — 动态注册时的 client URI。
     * ``oauth_redirect_uri`` — 回调 URI。
     * ``oauth_scope`` — Authorization scope。
     * ``oauth_device_id`` — 请求的设备 ID。
     * ``oauth_open_browser`` — 是否自动打开浏览器。
     * ``oauth_callback_timeout`` — 回调超时（秒）。

   **自动接受邀请**
     * ``auto_accept_invites`` — 是否启用自动接受。
     * ``auto_accept_whitelist`` — 白名单（``None`` 表示全员允许）。
     * ``auto_accept_blacklist`` — 黑名单（优先级高于白名单）。

   **E2EE 配置**
     * ``recovery_code`` — MATRIX_RECOVERY_CODE（密钥恢复私钥）。
     * ``e2ee_store_path`` — E2EE 持久化目录。

   运行时字段（一般不需要手动设置）:
     * ``session_type`` — ``"legacy_login"`` 或 ``"oauth2"``。
     * ``oauth_token_endpoint`` — OAuth2 refresh 端点。
