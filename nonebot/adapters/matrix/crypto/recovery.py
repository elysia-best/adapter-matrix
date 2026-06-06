"""KeyRecovery — 使用 MATRIX_RECOVERY_CODE 从服务端密钥备份恢复 Megolm 会话。

Matrix 支持将 Megolm 会话密钥加密上传到服务端密钥备份。
用户可以通过 recovery code（base58 编码的 Curve25519 私钥）解密备份的密钥，
以便在新设备或重装后恢复历史加密消息的解密能力。

备份加密算法: m.megolm_backup.v1.curve25519-aes-sha2
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

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
        backup_public_key = auth_data.get("public_key")
        if backup_public_key is None:
            log("WARNING", "备份 auth_data 缺少 public_key")
            return 0

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
                    session_key = self._decrypt_session_data(private_key_bytes, session_data)
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
        import base58 as base58_lib

        code = recovery_code.strip()
        # 移除可能的 PEM 页眉页脚
        if code.startswith("-----BEGIN"):
            lines = code.splitlines()
            code = "".join(
                line.strip()
                for line in lines
                if line.strip() and not line.startswith("-----")
            )

        # 去除空白字符（Matrix recovery code 通常以空格分组显示）
        code = re.sub(r"\s+", "", code)

        try:
            decoded = base58_lib.b58decode(code)
        except Exception as e:
            log("ERROR", f"base58 解码 recovery code 失败: {e}")
            msg = f"无效的 recovery code: {e}"
            raise ValueError(msg) from e

        # Matrix recovery key 结构 (Matrix Spec Appendices):
        #
        #   字节数组 = 0x8B || 0x01 || raw_key(32 bytes) || parity(1 byte)
        #
        #   其中:
        #   - 0x8B: 密钥类型标识 (Curve25519)
        #   - 0x01: 版本号
        #   - raw_key: Curve25519 私钥 (32 bytes)
        #   - parity: 全部前序字节的 XOR 校验和
        #
        #   部分旧实现可能省略版本号或校验字节。
        key = decoded
        log("TRACE", f"recovery key 解码后共 {len(key)} 字节 (hex: {key.hex()})")

        # 移除 0x8B 0x01 双字节头 (Matrix Spec 格式)
        if len(key) >= 34 and key[0] == 0x8B and key[1] == 0x01:
            key = key[2:]  # 移除双字节头
            # 如果末尾还有校验字节 (总长 35 的情况)，移除末尾 1 字节
            if len(key) > 32:
                key = key[:32]
        # 回退: 仅移除 0x8B 单字节头 (旧实现)
        elif len(key) >= 33 and key[0] == 0x8B:
            key = key[1:]  # 移除单字节头
            if len(key) > 32:
                key = key[:32]

        if len(key) != 32:
            log(
                "WARNING",
                f"recovery key 解码后长度为 {len(key)} (期望 32)",
            )
            # 仍尝试使用
            if len(key) > 32:
                key = key[:32]

        return key
