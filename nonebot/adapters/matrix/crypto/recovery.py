"""KeyRecovery — 使用 MATRIX_RECOVERY_CODE 从服务端密钥备份恢复 Megolm 会话。

Matrix 支持将 Megolm 会话密钥加密上传到服务端密钥备份。
用户可以通过 recovery code（base58 编码的 Curve25519 私钥）解密备份的密钥，
以便在新设备或重装后恢复历史加密消息的解密能力。

备份加密算法: m.megolm_backup.v1.curve25519-aes-sha2
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .secret_storage import decode_recovery_key
from ..utils import log

if TYPE_CHECKING:
    from .megolm import MegolmManager
    from .store import CryptoStore
    from ..adapter import Adapter
    from ..bot import Bot


class KeyRecovery:
    """从服务端密钥备份恢复 Megolm 会话。

    使用 recovery code 解密备份的 session_data，
    提取 session_key 并导入到 MegolmManager。
    """

    # HKDF salt: 32 字节的零值
    _HKDF_SALT = b"\x00" * 32

    def __init__(self, store: CryptoStore, megolm_mgr: MegolmManager) -> None:
        self._store = store
        self._megolm = megolm_mgr

    async def recover_from_secret_storage(
        self, adapter: Adapter, bot: Bot, passphrase: str
    ) -> int:
        """Unlock SSSS and recover the encrypted Megolm backup key.

        Homeservers expose SSSS as account-data events.  This method only
        reads those events and the existing room-key backup; it never creates,
        rotates, or uploads a backup.
        """
        from .secret_storage import decrypt_secret, derive_passphrase_key

        default = await adapter._api_get_account_data(  # type: ignore[union-attr]
            bot, event_type="m.secret_storage.default_key"
        )
        key_id = default.get("key") or default.get("default_key")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("Secret Storage has no default key")
        key_event = await adapter._api_get_account_data(  # type: ignore[union-attr]
            bot, event_type=f"m.secret_storage.key.{key_id}"
        )
        passphrase_data = key_event.get("passphrase")
        if not isinstance(passphrase_data, dict):
            raise ValueError("Secret Storage key has no passphrase metadata")
        salt = passphrase_data.get("salt")
        iterations = passphrase_data.get("iterations", 500_000)
        if not isinstance(salt, str) or not isinstance(iterations, int):
            raise ValueError("invalid Secret Storage passphrase metadata")
        key = derive_passphrase_key(
            passphrase,
            salt=salt,
            iterations=iterations,
            bits=int(passphrase_data.get("bits", 256)),
        )
        # Some clients include an encrypted copy of the SSSS key in the key
        # event.  When absent, the PBKDF2 result is itself the SSSS key.
        encrypted_key = key_event.get("encrypted")
        if isinstance(encrypted_key, dict):
            key = decrypt_secret(key, encrypted_key)
        secret = await adapter._api_get_account_data(  # type: ignore[union-attr]
            bot, event_type="m.megolm_backup.v1"
        )
        encrypted_secrets = secret.get("encrypted")
        encrypted_secret = (
            encrypted_secrets.get(key_id)
            if isinstance(encrypted_secrets, dict)
            else None
        )
        if not isinstance(encrypted_secret, dict):
            raise ValueError("Megolm backup secret is not stored in Secret Storage")
        backup = decrypt_secret(key, encrypted_secret)
        try:
            payload = json.loads(backup.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid Megolm backup secret") from exc
        private_value = (
            payload.get("private_key") if isinstance(payload, dict) else None
        )
        if not isinstance(private_value, str):
            raise ValueError("Megolm backup secret has no private key")
        private_key = self._b64decode(private_value)
        if len(private_key) != 32:
            raise ValueError("Megolm backup private key must be 32 bytes")
        import base58

        recovery_code = base58.b58encode(b"\x8b\x01" + private_key).decode("ascii")
        return await self.recover_from_backup(adapter, bot, recovery_code)

    async def recover_from_backup(
        self, adapter: Adapter, bot: Bot, recovery_code: str
    ) -> int:
        """使用恢复码从服务端备份恢复 Megolm 会话密钥。

        恢复流程:
        1. 解码 recovery code → Curve25519 私钥
        2. 获取最新备份版本信息 (GET /room_keys/version)
        3. 下载所有加密的会话密钥 (GET /room_keys/keys)
        4. 用私钥解密每个 session_data
        5. 将解密后的 session_key 导入 MegolmManager

        Args:
            adapter: Adapter 实例
            bot: Bot 实例
            recovery_code: 用户提供的恢复码

        Returns:
            成功恢复的会话密钥数量
        """
        # 解码 recovery code
        private_key_bytes = self._decode_recovery_key(recovery_code)

        # 获取备份版本
        try:
            version_info = await adapter._api_room_keys_version(bot)  # type: ignore[union-attr]
        except Exception as e:
            log("ERROR", f"获取密钥备份版本失败: {type(e).__name__}: {e}")
            return 0

        version = version_info.get("version")
        if version is None:
            log("WARNING", "没有可用的密钥备份版本")
            return 0

        auth_data = version_info.get("auth_data", {})
        if not isinstance(auth_data, dict):
            log("WARNING", "备份 auth_data 格式无效")
            return 0
        backup_public_key = auth_data.get("public_key")
        if not isinstance(backup_public_key, str):
            log("WARNING", "备份 auth_data 缺少 public_key")
            return 0
        actual_public_key = (
            base64.b64encode(
                X25519PrivateKey.from_private_bytes(private_key_bytes)
                .public_key()
                .public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            )
            .decode("ascii")
            .rstrip("=")
        )

        def normalise(value: str) -> str:
            return value.replace("+", "-").replace("/", "_").rstrip("=")

        if normalise(actual_public_key) != normalise(backup_public_key):
            raise ValueError("recovery key does not match backup public key")

        log("INFO", f"正在从密钥备份版本 {version} 恢复会话...")

        # 获取所有加密的会话密钥
        try:
            keys_response = await adapter._api_room_keys_keys(  # type: ignore[union-attr]
                bot, version=version
            )
        except Exception as e:
            log("ERROR", f"获取备份密钥数据失败: {type(e).__name__}: {e}")
            return 0

        recovered = 0
        rooms = keys_response.get("rooms", {})
        for room_id, room_data in rooms.items():
            if not isinstance(room_data, dict):
                continue
            sessions = room_data.get("sessions", {})
            if not isinstance(sessions, dict):
                continue
            for session_id, session_info in sessions.items():
                if not isinstance(session_info, dict):
                    continue
                session_data = session_info.get("session_data", {})
                try:
                    session_key = self._decrypt_session_data(
                        private_key_bytes, session_data
                    )
                    if session_key:
                        if self._megolm.add_inbound_session(
                            room_id, session_id, session_key
                        ):
                            recovered += 1
                except Exception as e:
                    log(
                        "WARNING",
                        f"恢复密钥 {room_id}/{session_id} 失败: "
                        f"{type(e).__name__}: {e}",
                    )

        log("INFO", f"从密钥备份恢复了 {recovered} 个 Megolm 会话密钥")
        return recovered

    async def recover_session_from_backup(
        self,
        adapter: Adapter,
        bot: Bot,
        recovery_code: str,
        room_id: str,
        session_id: str,
    ) -> bool:
        """Fetch and import one missing room key, then let decryption retry."""
        private_key = self._decode_recovery_key(recovery_code)
        version_info = await adapter._api_room_keys_version(bot)  # type: ignore[union-attr]
        version = version_info.get("version")
        if not isinstance(version, str):
            return False
        auth_data = version_info.get("auth_data")
        backup_public_key = auth_data.get("public_key") if isinstance(auth_data, dict) else None
        if not isinstance(backup_public_key, str):
            return False
        actual_public_key = base64.b64encode(
            X25519PrivateKey.from_private_bytes(private_key)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ).decode("ascii").rstrip("=")

        def normalise(value: str) -> str:
            return value.replace("+", "-").replace("/", "_").rstrip("=")

        if normalise(actual_public_key) != normalise(backup_public_key):
            return False
        response = await adapter._api_room_keys_keys(  # type: ignore[union-attr]
            bot, room_id=room_id, session_id=session_id, version=version
        )
        session_info = response.get("session_data")
        if not isinstance(session_info, dict):
            session_info = (
                response.get("rooms", {})
                .get(room_id, {})
                .get("sessions", {})
                .get(session_id, {})
                .get("session_data")
            )
        if not isinstance(session_info, dict):
            return False
        key = self._decrypt_session_data(private_key, session_info)
        return bool(key and self._megolm.add_inbound_session(room_id, session_id, key))

    # ------------------------------------------------------------------
    # 备份解密算法: m.megolm_backup.v1.curve25519-aes-sha2
    # ------------------------------------------------------------------

    def _decrypt_session_data(
        self, private_key_bytes: bytes, session_data: dict[str, Any]
    ) -> str | None:
        """解密单个 session_data，返回 session_key。

        解密步骤 (参见 Matrix Spec):
        1. 从 private_key 和 ephemeral key 进行 ECDH → shared_secret
        2. HKDF-SHA256(shared_secret, salt=zeros, info="") → 80 bytes:
           [0:32] AES-256-CBC key
           [32:64] HMAC-SHA256 key
           [64:80] IV
        3. 验证 MAC
        4. AES-256-CBC 解密 (PKCS#7 填充)
        """
        ciphertext_b64 = session_data.get("ciphertext")
        ephemeral_b64 = session_data.get("ephemeral")
        mac_b64 = session_data.get("mac")

        if not (
            isinstance(ciphertext_b64, str)
            and isinstance(ephemeral_b64, str)
            and isinstance(mac_b64, str)
        ):
            log("WARNING", "session_data 缺少必要字段")
            return None

        try:
            ciphertext = self._b64decode(ciphertext_b64)
            ephemeral = self._b64decode(ephemeral_b64)
            expected_mac = self._b64decode(mac_b64)
        except Exception as e:
            log("WARNING", f"session_data base64 解码失败: {e}")
            return None

        # 验证 ephemeral key 长度 (Curve25519 公钥 = 32 bytes)
        if len(ephemeral) != 32:
            log("WARNING", f"ephemeral key 长度异常: {len(ephemeral)}")
            return None

        # ECDH: private_key * ephemeral_public → shared_secret
        try:
            private_key = X25519PrivateKey.from_private_bytes(private_key_bytes)
            ephemeral_public = X25519PublicKey.from_public_bytes(ephemeral)
            shared_secret = private_key.exchange(ephemeral_public)
        except Exception as e:
            log("WARNING", f"ECDH 密钥协商失败: {e}")
            return None

        # HKDF-SHA256 派生 80 字节密钥材料
        hkdf = HKDF(
            algorithm=SHA256(),
            length=80,
            salt=self._HKDF_SALT,
            info=b"",
        )
        key_material = hkdf.derive(shared_secret)

        aes_key = key_material[0:32]
        mac_key = key_material[32:64]
        aes_iv = key_material[64:80]

        # 验证 MAC: HMAC-SHA256(ciphertext, mac_key)[:8] == mac
        import hmac as hmac_mod

        computed_hmac = hmac_mod.HMAC(mac_key, ciphertext, "sha256")
        actual_mac = computed_hmac.digest()[:8]  # 取前 8 字节

        # 安全的 MAC 比较 (预防时序攻击)
        if not hmac_mod.compare_digest(
            actual_mac,
            expected_mac[:8] if len(expected_mac) >= 8 else expected_mac,
        ):
            log("WARNING", "session_data MAC 验证失败")
            return None

        # AES-256-CBC 解密
        try:
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv))
            decryptor = cipher.decryptor()
            padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        except Exception as e:
            log("WARNING", f"AES 解密失败: {e}")
            return None

        # 移除 PKCS#7 填充
        plaintext = self._unpad_pkcs7(padded_plaintext)
        if plaintext is None:
            return None

        # 解码 JSON → 提取 session_key
        try:
            key_data = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log("WARNING", f"session_data JSON 解析失败: {e}")
            return None

        return key_data.get("session_key")

    @staticmethod
    def _unpad_pkcs7(data: bytes) -> bytes | None:
        """移除 PKCS#7 填充。"""
        if not data:
            return None
        padding_length = data[-1]
        if padding_length < 1 or padding_length > 16:
            return None
        if data[-padding_length:] != bytes([padding_length] * padding_length):
            return None
        return data[:-padding_length]

    @staticmethod
    def _b64decode(s: str) -> bytes:
        """Decode base64 string, with or without padding.

        Python's b64decode requires '=' padding, but many Matrix
        implementations (e.g. Element, matrix-js-sdk) store base64
        data without trailing padding characters.
        """
        s = s.strip().replace("-", "+").replace("_", "/")
        # Add missing padding
        remainder = len(s) % 4
        if remainder:
            s += "=" * (4 - remainder)
        return base64.b64decode(s)

    # ------------------------------------------------------------------
    # Recovery Code 解码
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_recovery_key(recovery_code: str) -> bytes:
        """解码 Matrix recovery code 为原始私钥字节。

        Recovery code 是 base58 (Bitcoin-style) 编码的 Curve25519 私钥，
        可能包含 PEM 风格的页眉页脚:
            -----BEGIN MATRIX PRIVATE KEY-----
            <base58 data>
            -----END MATRIX PRIVATE KEY-----
        """
        return decode_recovery_key(recovery_code)
