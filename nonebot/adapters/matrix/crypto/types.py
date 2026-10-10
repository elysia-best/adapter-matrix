"""Public E2EE states, errors and asynchronous subscriptions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import Enum
from typing import Generic, TypeVar


class CryptoError(Exception):
    """An encryption operation failed; sending plaintext is not a fallback."""


class StoreError(CryptoError):
    """Persistent state cannot be safely opened or committed."""


class DecryptionError(CryptoError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class LocalTrust(str, Enum):
    VERIFIED = "Verified"
    BLACKLISTED = "BlackListed"
    IGNORED = "Ignored"
    UNSET = "Unset"


class CollectStrategy(str, Enum):
    ALL_DEVICES = "AllDevices"
    ERROR_ON_VERIFIED_USER_PROBLEM = "ErrorOnVerifiedUserProblem"
    IDENTITY_BASED = "IdentityBasedStrategy"
    ONLY_TRUSTED_DEVICES = "OnlyTrustedDevices"


class BackupDownloadStrategy(str, Enum):
    MANUAL = "Manual"
    ONE_SHOT = "OneShot"
    AFTER_DECRYPTION_FAILURE = "AfterDecryptionFailure"


class VerificationState(str, Enum):
    UNKNOWN = "Unknown"
    VERIFIED = "Verified"
    UNVERIFIED = "Unverified"


class RecoveryState(str, Enum):
    UNKNOWN = "Unknown"
    ENABLED = "Enabled"
    DISABLED = "Disabled"
    INCOMPLETE = "Incomplete"


class BackupState(str, Enum):
    UNKNOWN = "Unknown"
    ENABLING = "Enabling"
    ENABLED = "Enabled"
    RESUMING = "Resuming"
    DOWNLOADING = "Downloading"
    DISABLING = "Disabling"
    CREATING = "Creating"


T = TypeVar("T")


class TaskLock:
    """Serialize mutations while allowing replies from the current sync task."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0

    async def __aenter__(self) -> TaskLock:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Crypto operations require an asyncio task")
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()


class Observable(Generic[T]):
    """Subscribe before taking a snapshot so no transition can be missed."""

    def __init__(self, value: T) -> None:
        self.value = value
        self._listeners: set[asyncio.Queue[T]] = set()

    def set(self, value: T) -> None:
        self.value = value
        for listener in tuple(self._listeners):
            listener.put_nowait(value)

    async def changes(self) -> AsyncIterator[T]:
        queue: asyncio.Queue[T] = asyncio.Queue()
        self._listeners.add(queue)
        try:
            yield self.value
            while True:
                yield await queue.get()
        finally:
            self._listeners.discard(queue)
