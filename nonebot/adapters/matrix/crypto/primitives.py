"""Matrix encodings and libolm signatures (no ratchet implementation)."""

from __future__ import annotations

import base64
import json
from typing import Any

import olm

OLM_ALGORITHM = "m.olm.v1.curve25519-aes-sha2"
MEGOLM_ALGORITHM = "m.megolm.v1.aes-sha2"


def b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii").rstrip("=")


def b64d(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )


def canonical_json(value: Any) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, float) or (
            isinstance(item, int) and not -(2**53 - 1) <= item <= 2**53 - 1
        ):
            raise ValueError("value is outside Matrix canonical JSON")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("canonical JSON keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)

    validate(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def signing_json(value: dict[str, Any]) -> str:
    return canonical_json(
        {k: v for k, v in value.items() if k not in {"signatures", "unsigned"}}
    )


def sign_json(
    value: dict[str, Any], signer: Any, user_id: str, key_id: str
) -> dict[str, Any]:
    value.setdefault("signatures", {}).setdefault(user_id, {})[f"ed25519:{key_id}"] = (
        signer.sign(signing_json(value))
    )
    return value


def verify_json(
    value: dict[str, Any], user_id: str, key_id: str, public_key: str
) -> bool:
    try:
        signature = value["signatures"][user_id][f"ed25519:{key_id}"]
        olm.ed25519_verify(public_key, signing_json(value), signature)
    except (KeyError, TypeError, ValueError, olm.OlmVerifyError):
        return False
    return True
