事件（Event）
===============

Matrix 适配器将 homeserver 推送的各类事件映射为 NoneBot ``Event`` 子类，供事件处理器使用。

.. automodule:: nonebot.adapters.matrix.event
   :no-index:

事件类型
--------

``EventType`` 是事件的三大分类：

.. code-block:: python

   EventType = Literal["message", "notice", "meta"]

- ``message`` — 消息类事件（房间文本、图片等）。
- ``notice`` — 通知类事件（成员变动、反应、输入状态等）。
- ``meta`` — 元事件（同步状态等）。

事件层次
--------

.. code-block:: text

   Event (基类)
   ├── MetaEvent           —— 元事件
   │   └── SyncMetaEvent   —— 同步元事件
   ├── NoticeEvent         —— 通知事件
   │   ├── MessageEvent    —— ⚠ 注意继承链，见下
   │   ├── RoomMemberEvent —— 房间成员变动
   │   ├── ReactionEvent   —— 表情反应
   │   ├── RedactionEvent  —— 消息撤回
   │   ├── TypingEvent     —— 输入状态
   │   ├── ReceiptEvent    —— 已读回执
   │   ├── UnknownRoomEvent—— 未识别的房间事件
   │   ├── InviteEvent     —— 房间邀请
   │   └── LeaveEvent      —— 离开房间
   └── (NoticeEvent)
       ├── RoomMessageEvent    —— 普通房间消息
       └── EncryptedRoomEvent —— Megolm 加密的房间消息

基类 :class:`Event`
--------------------

所有 Matrix 事件的通用基类，包含以下核心字段：

.. autoattribute:: Event.type
.. autoattribute:: Event.content
.. autoattribute:: Event.event_id
.. autoattribute:: Event.sender
.. autoattribute:: Event.room_id
.. autoattribute:: Event.origin_server_ts
.. autoattribute:: Event.to_me
.. autoattribute:: Event.state_key

常用方法：

.. automethod:: Event.get_type
.. automethod:: Event.get_user_id
.. automethod:: Event.get_session_id
.. automethod:: Event.get_message
.. automethod:: Event.is_tome
.. automethod:: Event.get_event_description

消息事件 :class:`MessageEvent`
--------------------------------

``MessageEvent`` 是插件中最常用的事件类型，表示一条 Matrix 房间消息。

.. autoattribute:: MessageEvent.reply

   回复目标事件 ID。

.. automethod:: MessageEvent.get_message

   返回消息的 :class:`Message` 对象。

房间消息 :class:`RoomMessageEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

普通的 ``m.room.message`` 事件。

加密消息 :class:`EncryptedRoomEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``m.room.encrypted`` 事件。解密成功后，``content`` 会被替换为明文内容，原始加密内容保留在 ``encrypted_content`` 中。

.. autoattribute:: EncryptedRoomEvent.encrypted_content

通知事件
--------

成员变动 :class:`RoomMemberEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoattribute:: RoomMemberEvent.membership

   成员状态：``"join"`` / ``"invite"`` / ``"leave"`` / ``"ban"``。

反应 :class:`ReactionEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoattribute:: ReactionEvent.relates_to
.. autoattribute:: ReactionEvent.target_event_id
.. autoattribute:: ReactionEvent.key

   反应 key（如 ``"👍"``）。

撤回 :class:`RedactionEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoattribute:: RedactionEvent.redacts

   被撤回的事件 ID。

输入状态 :class:`TypingEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoattribute:: TypingEvent.user_ids

   当前房间内正在输入的用户 ID 列表。

邀请 :class:`InviteEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~~~

收到新的房间邀请时触发。

离开 :class:`LeaveEvent`
~~~~~~~~~~~~~~~~~~~~~~~~~

用户离开或被踢出房间时触发。

事件分发映射
------------

``event_classes`` 字典将 Matrix 事件类型映射到对应的 Event 子类：

.. code-block:: python

   event_classes: dict[str, type[Event]] = {
       "m.room.message": RoomMessageEvent,
       "m.room.encrypted": EncryptedRoomEvent,
       "m.room.member": RoomMemberEvent,
       "m.reaction": ReactionEvent,
       "m.room.redaction": RedactionEvent,
       "m.typing": TypingEvent,
       "m.receipt": ReceiptEvent,
   }

未在映射表中的事件类型将使用 ``UnknownRoomEvent``。
