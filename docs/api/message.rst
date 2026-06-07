消息（Message）
===============

Matrix 适配器的消息系统由 :class:`MessageSegment` （消息段）和 :class:`Message` （消息）两层组成，遵循 NoneBot 标准消息模型。

.. automodule:: nonebot.adapters.matrix.message
   :no-index:

消息段 :class:`MessageSegment`
-------------------------------

每种消息段对应一种 Matrix 消息格式。所有构造方法都是 ``@staticmethod``，返回对应子类的实例。

文本类消息段
   - :meth:`MessageSegment.text` — 普通文本消息（``m.text``）。
   - :meth:`MessageSegment.notice` — 通知消息（``m.notice``），通常不以通知推送方式显示。
   - :meth:`MessageSegment.emote` — 表情动作（``m.emote``），如 ``/me 做了某事``。
   - :meth:`MessageSegment.html` — HTML 格式文本，需同时提供纯文本和格式化文本。
   - :meth:`MessageSegment.mention_user` — @ 提及用户，自动生成 ``m.mentions`` 和 Matrix.to 链接。

媒体类消息段
   以下方法在传入 ``bytes`` 时会**自动上传**到 homeserver（需要 ``bot`` 实例）；传入 ``mxc://`` URI 则直接引用。

   - :meth:`MessageSegment.image` — 图片（``m.image``）。
   - :meth:`MessageSegment.file` — 文件（``m.file``）。
   - :meth:`MessageSegment.audio` — 音频（``m.audio``）。
   - :meth:`MessageSegment.video` — 视频（``m.video``）。

   公共参数：
     * **content** -- 二进制内容（``bytes``）、``mxc://`` URI，或文件名。
     * **filename** -- 文件名（可选）。
     * **body** -- 替代文本（可选）。
     * **content_type** -- MIME Type（可选，强烈建议填写）。
     * **info** -- 额外元数据（可选，如宽高、时长）。

特殊消息段
   - :meth:`MessageSegment.reply` — 回复引用，绑定到指定事件。
   - :meth:`MessageSegment.raw` — 原始 Matrix 消息体（用于非标准消息类型）。

消息 :class:`Message`
----------------------

``Message`` 是 ``MessageSegment`` 的列表容器，支持 ``+`` 拼接多个消息段。

常用方法：

.. automethod:: Message.extract_plain_text

   提取消息中所有文本类消息段的纯文本，拼接为字符串。

.. automethod:: Message.clone

   深拷贝消息。

.. automethod:: Message.sendable

   判断消息是否可发送（非空）。

消息解析函数
------------

.. autofunction:: message_from_content
.. autofunction:: build_message_content
.. autofunction:: parse_message
