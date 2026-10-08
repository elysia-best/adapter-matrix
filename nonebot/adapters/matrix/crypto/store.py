"""Versioned JSON persistence for the architecture-independent crypto engine."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from .primitives import IdentityAccount


class CryptoStore:
    """Persist crypto state without serialising executable/native objects."""

    VERSION = 1

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        legacy = [
            path.name
            for path in self._dir.iterdir()
            if path.is_file()
            and (path.suffix == ".pickle" or path.name.startswith("olm_"))
        ]
        if legacy:
            names = ", ".join(sorted(legacy))
            raise RuntimeError(
                "legacy non-standard crypto store detected ("
                f"{names}); configure a new Matrix device identity and store path"
            )
        sessions_file = self._dir / "sessions.json"
        if sessions_file.exists():
            try:
                sessions = json.loads(sessions_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                sessions = None
            if isinstance(sessions, dict) and any(
                isinstance(value, dict)
                and isinstance(value.get("data"), str)
                and "type" in value
                for value in sessions.values()
            ):
                msg = (
                    "legacy non-standard Olm sessions detected; configure a new "
                    "Matrix device identity and store path"
                )
                raise RuntimeError(msg)

    def load_account(self) -> IdentityAccount | None:
        value = self._load_json("account.json", None)
        if not isinstance(value, dict) or value.get("version") != self.VERSION:
            return None
        try:
            return IdentityAccount.from_dict(value)
        except (KeyError, TypeError, ValueError):
            return None

    def save_account(self, account: IdentityAccount) -> None:
        if isinstance(account, IdentityAccount):
            self._save_json("account.json", account.to_dict())

    def load_sessions(self) -> dict[str, dict[str, Any]]:
        value = self._load_json("sessions.json", {})
        return value if isinstance(value, dict) else {}

    def save_sessions(self, sessions: dict[str, Any]) -> None:
        serialisable = {
            key: value.to_dict() if hasattr(value, "to_dict") else value
            for key, value in sessions.items()
            if hasattr(value, "to_dict") or isinstance(value, dict)
        }
        self._save_json("sessions.json", serialisable)

    def load_inbound_sessions(self) -> dict[str, dict[str, str]]:
        value = self._load_json("inbound_sessions.json", {})
        return value if isinstance(value, dict) else {}

    def save_inbound_sessions(self, sessions: dict[str, dict[str, str]]) -> None:
        self._save_json("inbound_sessions.json", sessions)

    def load_outbound_sessions(self) -> dict[str, dict[str, Any]]:
        value = self._load_json("outbound_sessions.json", {})
        return value if isinstance(value, dict) else {}

    def save_outbound_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        self._save_json("outbound_sessions.json", sessions)

    def load_device_keys(self) -> dict[str, dict[str, dict[str, Any]]]:
        value = self._load_json("device_keys.json", {})
        return value if isinstance(value, dict) else {}

    def save_device_keys(self, keys: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._save_json("device_keys.json", keys)

    def load_room_state(self) -> dict[str, dict[str, Any]]:
        value = self._load_json("room_state.json", {})
        return value if isinstance(value, dict) else {}

    def save_room_state(self, state: dict[str, dict[str, Any]]) -> None:
        self._save_json("room_state.json", state)

    def _load_json(self, name: str, default: Any) -> Any:
        path = self._dir / name
        try:
            return (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else default
            )
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, name: str, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        self._atomic_write(self._dir / name, payload)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        tmp.replace(path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
