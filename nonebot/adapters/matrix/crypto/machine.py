"""Read-only access to the engine's durable outgoing request queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import CryptoStore


@dataclass(frozen=True)
class OutgoingRequest:
    request_id: str
    kind: str
    body: dict[str, Any]


class CryptoMachine:
    """Queue inspection; the engine acknowledges responses transactionally."""

    def __init__(self, store: CryptoStore) -> None:
        self.store = store

    def outgoing_requests(self) -> list[OutgoingRequest]:
        result = []
        for name in self.store.names("outgoing/"):
            record = self.store.get(name)
            result.append(
                OutgoingRequest(
                    name.removeprefix("outgoing/"), record["api"], record["body"]
                )
            )
        return result
