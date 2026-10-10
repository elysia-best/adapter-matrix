"""The one missing python3-olm operation: importing a backup private key.

Use the CFFI already shipped by upstream python3-olm, never load a second libolm
or implement its public-key encryption algorithm in Python.
"""

from __future__ import annotations

from _libolm import ffi, lib
from olm.pk import PkDecryption

from .types import CryptoError


def pk_from_private(private_key: bytes) -> PkDecryption:
    required = (
        "olm_get_library_version",
        "olm_pk_key_from_private",
        "olm_pk_private_key_length",
        "olm_pk_key_length",
    )
    if not all(hasattr(lib, name) for name in required):
        raise CryptoError("python3-olm with the libolm PK import API is required")
    major, minor, patch = (ffi.new("uint8_t *") for _ in range(3))
    lib.olm_get_library_version(major, minor, patch)
    if (major[0], minor[0], patch[0]) < (3, 2, 0):
        raise CryptoError("libolm 3.2 or later is required")
    if len(private_key) != lib.olm_pk_private_key_length():
        raise ValueError("Backup private key must contain 32 bytes")
    obj = PkDecryption.__new__(PkDecryption)
    length = lib.olm_pk_key_length()
    public = ffi.new("char[]", length)
    secret = ffi.new("char[]", private_key)
    try:
        # Upstream initializes this CFFI handle in __new__ without a type declaration.
        result = lib.olm_pk_key_from_private(
            obj._pk_decryption,  # pyright: ignore[reportAttributeAccessIssue]
            public,
            length,
            secret,
            len(private_key),
        )
        if result == lib.olm_error():
            raise CryptoError("libolm rejected the backup private key")
        obj.public_key = ffi.unpack(public, length).decode("ascii")
    finally:
        ffi.buffer(secret)[:] = bytes(len(ffi.buffer(secret)))
    return obj
