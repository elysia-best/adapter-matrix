"""Transactional, authenticated storage for libolm state.

All methods are synchronous and must run on the owning event loop. Transactions
never span an await; the engine serializes network-dependent mutations with its
async lock. No Python executable pickle is ever deserialized.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import olm

from .primitives import b64e
from .types import StoreError


def private_permissions(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        # chmod on Windows only controls the read-only bit. Replace inherited
        # ACLs with an explicit full-control grant for the current principal.
        principal = subprocess.check_output(["whoami"], text=True).strip()
        grant = f"{principal}:(OI)(CI)F" if directory else f"{principal}:F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            check=True,
            capture_output=True,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


class CryptoStore:
    VERSION = 1

    def __init__(self, store_dir: Path, identity: tuple[str, str, str]) -> None:
        if len(identity) != 3 or not all(identity):
            raise StoreError(
                "Crypto store requires a homeserver, user ID and device ID"
            )
        self.path = store_dir
        legacy = [
            p.name
            for p in store_dir.glob("*")
            if p.suffix in {"json", ".json", ".pickle"}
        ]
        if legacy:
            raise StoreError(
                "Legacy crypto state detected; use a new device and store directory"
            )
        store_dir.mkdir(parents=True, exist_ok=True)
        private_permissions(store_dir, directory=True)
        key_path = store_dir / "store.key"
        database_path = store_dir / "crypto.sqlite3"
        self.new_database = not database_path.exists()
        if database_path.exists() and not key_path.exists():
            raise StoreError(
                "Crypto store key is missing; refusing to replace the device identity"
            )
        try:
            with key_path.open("xb") as stream:
                private_permissions(key_path)
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        key = key_path.read_bytes()
        if len(key) != 32:
            raise StoreError("Invalid crypto store key")
        private_permissions(key_path)
        self.pickle_key = b64e(key)
        self._cipher = AESGCM(key)
        self._depth = 0
        try:
            self._db = sqlite3.connect(database_path, isolation_level=None)
            private_permissions(database_path)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version != (0 if self.new_database else self.VERSION):
                raise StoreError("Unsupported crypto store version")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS records (name TEXT PRIMARY KEY, value BLOB NOT NULL)"
            )
            self._db.execute(f"PRAGMA user_version={self.VERSION}")
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreError("Corrupt crypto database")
            with self.transaction():
                existing = self.get("identity")
                expected = list(identity)
                if not self.new_database and (
                    existing is None or self.get("canary") != "matrix-libolm-v1"
                ):
                    raise StoreError(
                        "Incomplete crypto store; use a new device and store directory"
                    )
                if existing is not None and expected != existing:
                    raise StoreError(
                        "Crypto store belongs to a different homeserver, user or device"
                    )
                if existing is None:
                    self.put("identity", expected)
                # Authenticates even a test store without a configured identity.
                if self.get("canary", "matrix-libolm-v1") != "matrix-libolm-v1":
                    raise StoreError("Invalid crypto store")
                self.put("canary", "matrix-libolm-v1")
        except (sqlite3.Error, InvalidTag, ValueError, StoreError) as exc:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(exc, StoreError):
                raise
            raise StoreError("Cannot open authenticated crypto store") from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        name = f"crypto_{self._depth}"
        self._db.execute(f"SAVEPOINT {name}")
        self._depth += 1
        try:
            yield
            self._db.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            self._db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._db.execute(f"RELEASE SAVEPOINT {name}")
            raise
        finally:
            self._depth -= 1

    def get(self, name: str, default: Any = None) -> Any:
        try:
            row = self._db.execute(
                "SELECT value FROM records WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                return default
            data = row[0]
            return json.loads(self._cipher.decrypt(data[:12], data[12:], name.encode()))
        except (sqlite3.Error, InvalidTag, ValueError) as exc:
            raise StoreError(f"Cannot authenticate crypto record {name}") from exc

    def put(self, name: str, value: Any) -> None:
        nonce = os.urandom(12)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        encrypted = nonce + self._cipher.encrypt(nonce, data, name.encode())
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO records(name,value) VALUES (?,?)",
                (name, encrypted),
            )
        except sqlite3.Error as exc:
            raise StoreError(f"Cannot persist crypto record {name}") from exc

    def delete(self, name: str) -> None:
        self._db.execute("DELETE FROM records WHERE name=?", (name,))

    def names(self, prefix: str) -> list[str]:
        return [
            row[0]
            for row in self._db.execute("SELECT name FROM records ORDER BY name")
            if row[0].startswith(prefix)
        ]

    def load_account(self) -> olm.Account | None:
        value = self.get("account")
        if value is None:
            return None
        try:
            return olm.Account.from_pickle(value.encode("ascii"), self.pickle_key)
        except (olm.OlmAccountError, ValueError) as exc:
            raise StoreError("Invalid libolm account pickle") from exc

    def save_account(self, account: olm.Account) -> None:
        self.put("account", account.pickle(self.pickle_key).decode("ascii"))

    def close(self) -> None:
        self._db.close()
