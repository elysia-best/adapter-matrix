"""CryptoStore — E2EE 加密状态持久化存储。

使用 olm 原生的 pickle()/from_pickle() 方法序列化 C 扩展对象，
纯数据使用 JSON 序列化。

存储目录布局:
  {store_dir}/
    account.pickle        — olm.Account.pickle() 的 bytes 输出
    sessions.json         — {session_id: {type: str, data: base64_str}}
    inbound_sessions.json  — Megolm 入站会话的 session_key 映射
    outbound_sessions.json — Megolm 出站会话的 session_key 映射
    device_keys.json      — 设备密钥缓存
    room_state.json       — 房间加密状态
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import olm


class CryptoStore:
    """管理 Olm 对象和 E2EE 元数据的持久化。"""

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # OlmAccount — 使用 olm 原生 pickle()/from_pickle()
    # ------------------------------------------------------------------

    def load_account(self) -> Any:
        path = self._dir / "account.pickle"
        if not path.exists():
            return None
        try:
            return olm.Account.from_pickle(path.read_bytes())
        except (OSError, olm.OlmAccountError):
            return None

    def save_account(self, account: Any) -> None:
        path = self._dir / "account.pickle"
        self._atomic_write(path, account.pickle())

    # ------------------------------------------------------------------
    # OlmSession — 使用 olm pickle，按 session_id 索引
    # pickle 返回 bytes，以 base64 编码存储为 JSON
    # ------------------------------------------------------------------

    def load_sessions(self) -> dict[str, Any]:
        path = self._dir / "sessions.json"
        if not path.exists():
            return {}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(stored, dict):
            return {}

        sessions: dict[str, Any] = {}
        for sid, entry in stored.items():
            if not isinstance(entry, dict):
                continue
            data = entry.get("data", "")
            stype = entry.get("type", "InboundSession")
            cls = olm.InboundSession if "Inbound" in stype else olm.OutboundSession
            try:
                sessions[sid] = cls.from_pickle(base64.b64decode(data.encode("utf-8")))
            except (olm.OlmSessionError, TypeError, AttributeError):
                # OutboundSession.from_pickle 在某些 python-olm 版本中存在 bug
                continue
        return sessions

    def save_sessions(self, sessions: dict[str, Any]) -> None:
        path = self._dir / "sessions.json"
        stored: dict[str, dict[str, str]] = {}
        for sid, session in sessions.items():
            if not hasattr(session, "pickle"):
                continue
            try:
                stored[sid] = {
                    "type": type(session).__name__,
                    "data": base64.b64encode(session.pickle()).decode("ascii"),
                }
            except olm.OlmSessionError:
                continue
        self._atomic_write(
            path,
            json.dumps(stored, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # Megolm 入站会话 (JSON — 只存储 session_key 以支持重建)
    # {room_id: {session_id: session_key}}
    # ------------------------------------------------------------------

    def load_inbound_sessions(self) -> dict[str, dict[str, str]]:
        return self._load_json("inbound_sessions.json", {})

    def save_inbound_sessions(self, sessions: dict[str, dict[str, str]]) -> None:
        self._save_json("inbound_sessions.json", sessions)

    # ------------------------------------------------------------------
    # Megolm 出站会话 (JSON)
    # {room_id: {"session_id": ..., "session_key": ..., "message_index": ...}}
    # ------------------------------------------------------------------

    def load_outbound_sessions(self) -> dict[str, dict[str, Any]]:
        return self._load_json("outbound_sessions.json", {})

    def save_outbound_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        self._save_json("outbound_sessions.json", sessions)

    # ------------------------------------------------------------------
    # 设备密钥缓存 (JSON)
    # {user_id: {device_id: {keys, algorithms, signatures, ...}}}
    # ------------------------------------------------------------------

    def load_device_keys(self) -> dict[str, dict[str, dict[str, Any]]]:
        return self._load_json("device_keys.json", {})

    def save_device_keys(self, keys: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._save_json("device_keys.json", keys)

    # ------------------------------------------------------------------
    # 房间加密状态 (JSON)
    # {room_id: {"encrypted": bool, "algorithm": str}}
    # ------------------------------------------------------------------

    def load_room_state(self) -> dict[str, dict[str, Any]]:
        return self._load_json("room_state.json", {})

    def save_room_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._save_json("room_state.json", state)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _load_json(self, name: str, default: Any) -> Any:
        path = self._dir / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, name: str, data: Any) -> None:
        path = self._dir / name
        self._atomic_write(
            path,
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode(
                "utf-8"
            ),
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
