"""API Key 的加密存储（Fernet 对称加密，密钥是 .env 里的 CONFIG_SECRET_KEY）。从 main.py 原样搬来。"""
import os

from cryptography.fernet import Fernet

from app import config  # noqa: F401  先加载 .env


# 功能6：配置加密。CONFIG_SECRET_KEY 用于加密存储 API Key（Fernet 对称加密）。
_config_secret = os.getenv("CONFIG_SECRET_KEY")


_fernet = Fernet(_config_secret.encode()) if _config_secret else None


def encrypt_secret(plain: str) -> str:
    """加密明文 API Key；未配置密钥时降级为明文存储（并告警）。"""
    if not plain:
        return ""
    if not _fernet:
        print("[CONFIG] 警告：未配置 CONFIG_SECRET_KEY，API Key 将明文存储")
        return plain
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    """解密 API Key；解密失败（如密钥变更）时返回空串。"""
    if not token:
        return ""
    if not _fernet:
        return token
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        print("[CONFIG] 警告：API Key 解密失败（密钥可能已变更）")
        return ""
