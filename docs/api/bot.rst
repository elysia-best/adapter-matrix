Bot（机器人）
===============

Bot 是 Matrix 适配器的核心类，继承自 NoneBot 的 ``Bot`` 基类和 API 客户端，封装了所有用户可见的 Matrix 操作。

.. automodule:: nonebot.adapters.matrix.bot
   :no-index:

核心 API
--------

发送消息
   - :meth:`Bot.send` — 标准消息发送，自动处理加密。
   - :meth:`Bot.send_to` — 向指定房间发送消息。
   - :meth:`Bot.send_event` — 发送任意类型的房间事件。

房间操作
   - :meth:`Bot.join_room` — 加入房间。
   - :meth:`Bot.react` — 发送消息反应（m.reaction）。
   - :meth:`Bot.redact` — 撤回消息。
   - :meth:`Bot.set_typing_state` — 设置输入状态。
   - :meth:`Bot.mark_read` — 标记已读。
   - :meth:`Bot.upload_media` — 上传媒体到 homeserver。

属性
----

.. autoattribute:: Bot.user_id
.. autoattribute:: Bot.device_id
.. autoattribute:: Bot.bot_info
.. autoattribute:: Bot.self_info
.. autoattribute:: Bot.next_batch
.. autoattribute:: Bot.crypto

方法参考
--------

.. automethod:: Bot.send

   向事件来源房间发送消息。消息类型遵循 Matrix 规范，支持纯文本、HTML、表情和通知。

   若房间启用了 E2EE，消息会自动用 Megolm 加密。

   参数：
     * **event** -- 必须为 ``MessageEvent`` 类型，需要包含 ``room_id``。
     * **message** -- 字符串 / ``Message`` / ``MessageSegment``。
     * ****kwargs** -- 额外参数（如 ``txn_id``）。

   返回：
     ``EventIdResponse`` -- 包含已发送事件的 ``event_id``。

.. automethod:: Bot.send_to

   向指定房间发送消息。与 ``send`` 相同，但显式指定 ``room_id``。

.. automethod:: Bot.send_event

   发送任意类型的 Matrix 房间事件。用于发送非 ``m.room.message`` 的事件（如 ``m.room.encrypted``）。

.. automethod:: Bot.react

   对指定事件发送表情反应（m.reaction annotation）。

   参数：
     * **room_id** -- 房间 ID。
     * **event_id** -- 目标事件 ID。
     * **key** -- 反应 key，如 ``"👍"``。
     * **txn_id** -- 事务 ID（可选，默认自动生成）。

.. automethod:: Bot.redact

   撤回（redact）指定事件。

   参数：
     * **room_id** -- 房间 ID。
     * **event_id** -- 要撤回的事件 ID。
     * **reason** -- 撤回原因（可选）。
     * **txn_id** -- 事务 ID（可选）。

.. automethod:: Bot.join_room

   加入 Matrix 房间。

   参数：
     * **room_id** -- 房间 ID 或别名。
     * **reason** -- 加入原因（可选）。

   返回：
     ``JoinRoomResponse``

.. automethod:: Bot.set_typing_state

   设置机器人的输入状态。

   参数：
     * **room_id** -- 房间 ID。
     * **typing** -- 是否正在输入。
     * **timeout** -- 超时时间（毫秒，可选）。

.. automethod:: Bot.mark_read

   向房间发送已读回执。

   参数：
     * **room_id** -- 房间 ID。
     * **event_id** -- 标记为已读的事件 ID。
     * **receipt_type** -- 回执类型，默认 ``"m.read"``。
     * **thread_id** -- 线程 ID（可选）。

.. automethod:: Bot.upload_media

   上传媒体内容到 Matrix homeserver 的 media repository。

   参数：
     * **content** -- 媒体二进制数据。
     * **filename** -- 文件名（可选）。
     * **content_type** -- MIME Type（可选）。

   返回：
     ``UploadResponse`` -- 包含 ``content_uri``（即 ``mxc://`` URI）。
