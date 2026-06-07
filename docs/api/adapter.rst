适配器（Adapter）
==================

``Adapter`` 是 Matrix 适配器的入口类，负责生命周期管理、同步循环和 Token 刷新。

.. automodule:: nonebot.adapters.matrix.adapter
   :members:
   :exclude-members: _get_handler_params, _parse_oauth_code

核心职责
--------

- **生命周期管理**：注册 ``on_ready`` 和 ``on_shutdown`` 钩子。
- **Bot 启动**：遍历配置中的每个 ``BotInfo``，完成认证引导后启动独立的同步循环。
- **同步循环**：通过 ``/sync`` long-poll 持续拉取事件，分发给 ``Bot.handle_event()``。
- **Token 管理**：处理 access token 的刷新、持久化和恢复。
- **E2EE 集成**：在每个同步周期中处理 ``device_lists`` 和 ``to_device`` 事件。
- **自动接受邀请**：按白名单/黑名单策略自动加入房间。

认证流程
--------

``Adapter`` 内部实现了完整的认证引导流程（``_bootstrap_with_refresh``）：

1. 若已有 ``access_token``，先通过 ``/account/whoami`` 校验。
2. 校验失败时依次尝试：refresh → 密码登录 → OAuth2 登录。
3. 若只有静态 token 而无凭据/refresh token，输出警告后继续（token 过期会失败）。

关键属性
--------

``Adapter.matrix_config``
   ``Config`` 实例，包含所有全局配置和 Bot 列表。

扩展点
------

``Adapter._call_api(api, **data)``
   NoneBot API 调用的路由入口，通过 ``api`` 名称分发到对应的 ``_api_*`` 方法。

   若要添加自定义 API，继承 ``Adapter`` 并实现 ``_api_<name>`` 方法即可。
