"""Authenticated encryption for user-owned notification credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import security


def _fernet() -> Fernet:
    secret = os.environ.get("NOTIFICATION_ENCRYPTION_KEY", "").strip()
    if not secret:
        if security.is_production():
            raise RuntimeError("生产环境必须配置 NOTIFICATION_ENCRYPTION_KEY")
        secret = os.environ.get("MYSQL_PASSWORD", "marketbrief-local-development")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_config(config: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(config, ensure_ascii=False).encode("utf-8"))


def decrypt_config(token: bytes | str) -> dict[str, Any]:
    raw = token.encode("utf-8") if isinstance(token, str) else token
    try:
        payload = json.loads(_fernet().decrypt(raw).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("通知凭据无法解密，请重新配置该渠道") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("通知凭据格式无效")
    return payload
