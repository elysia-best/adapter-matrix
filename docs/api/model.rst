数据模型（Model）
==================

Matrix 适配器内部使用 Pydantic 模型表示 API 请求和响应的数据结构。以下列举最重要的类型。

.. automodule:: nonebot.adapters.matrix.api.model
   :members:
   :exclude-members: model_config

类型工具
--------

.. automodule:: nonebot.adapters.matrix.api.types
   :members:

核心模型
--------

Sync 相关
   - :class:`SyncResponse` — ``/sync`` 的完整响应，包含 ``next_batch`` 和 ``rooms``。
   - :class:`RoomsSync` — 顶层 rooms 分组（``join`` / ``invite`` / ``leave``）。
   - :class:`JoinedRoomSync` — 已加入房间的同步数据（``state`` / ``timeline`` / ``ephemeral``）。
   - :class:`InvitedRoomSync` — 邀请状态房间数据（含 ``invite_state``）。
   - :class:`LeftRoomSync` — 已离开房间的同步数据。
   - :class:`Timeline` — 时间线事件列表。
   - :class:`State` — 状态事件列表。
   - :class:`AccountData` — 账户数据事件列表。
   - :class:`DeviceLists` — 设备列表变更。

事件与内容模型
   - :class:`RawMatrixEvent` — 从 API 响应的原始事件表示。
   - :class:`RoomMessageContent` — ``m.room.message`` 的消息体。
   - :class:`RoomMemberContent` — ``m.room.member`` 的成员信息。
   - :class:`ReactionContent` / :class:`ReactionRelation` — ``m.reaction`` 的内容和关系。
   - :class:`RedactionContent` — 消息撤回内容。
   - :class:`TypingContent` — 输入状态内容。
   - :class:`ReceiptContent` / :class:`ReceiptThread` — 已读回执内容。

API 响应
   - :class:`EventIdResponse` — ``PUT /send`` 等端点的返回值，包含 ``event_id``。
   - :class:`UploadResponse` — 媒体上传的返回值，包含 ``content_uri``。
   - :class:`WhoamiResponse` — ``/account/whoami`` 的响应。
   - :class:`LoginResponse` — 登录响应。
   - :class:`LoginFlowsResponse` — 支持的登录流程。
   - :class:`RefreshTokenResponse` — Token 刷新响应。
   - :class:`JoinRoomResponse` — 加入房间响应。
   - :class:`MembersChunkResponse` — 成员列表响应。
   - :class:`MessagesResponse` — 消息分页响应。
   - :class:`RelationsResponse` — 关系事件响应。
   - :class:`MediaConfigResponse` — 媒体仓库配置。

常用类型别名
------------

- ``UserId`` — 用户 ID（如 ``@alice:example.org``）
- ``RoomId`` — 房间 ID（如 ``!abc123:example.org``）
- ``RoomAlias`` — 房间别名（如 ``#general:example.org``）
- ``RoomIdentifier`` — ``RoomId | RoomAlias``
- ``EventId`` — 事件 ID
- ``EventType`` — ``str``，事件类型字符串
- ``TxnId`` — ``str``，事务 ID
- ``DeviceId`` — ``str``，设备 ID
- ``MxcUri`` — ``str``，Matrix Content URI
- ``PresenceState`` — ``"online" | "offline" | "unavailable"``
- ``ReceiptType`` — ``"m.read" | "m.read.private"``
- ``UNSET`` — 哨兵值，表示字段未设置

异常
----

.. automodule:: nonebot.adapters.matrix.exception
   :members:
   :undoc-members:
