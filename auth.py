"""Email OTP login and session helpers."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from typing import Any

from db import get_conn, utc_now
import security

SESSION_COOKIE = "mb_session"
CODE_TTL_SECONDS = 5 * 60
CODE_RATE_LIMIT_SECONDS = 60
CODE_MAX_ATTEMPTS = 5
SESSION_TTL_DAYS = 30
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not cleaned or not EMAIL_RE.match(cleaned):
        raise AuthError("请输入有效的邮箱地址")
    if len(cleaned) > 255:
        raise AuthError("邮箱地址过长")
    return cleaned


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip() not in {"0", "false", "False", "no", ""}


def send_login_email(email: str, code: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        print(f"[auth] SMTP 未配置，登录验证码 email={email} code={code}")
        return

    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("SMTP_FROM", "").strip() or user or "noreply@localhost"
    # 465 默认走 SMTPS；587 默认 STARTTLS
    use_ssl = _env_flag("SMTP_USE_SSL", "1" if port == 465 else "0")
    use_tls = _env_flag("SMTP_USE_TLS", "0" if use_ssl else "1")

    message = EmailMessage()
    message["Subject"] = "MarketBrief_AI 登录验证码"
    message["From"] = from_addr
    message["To"] = email
    message.set_content(
        f"你的登录验证码是：{code}\n\n"
        f"验证码 {CODE_TTL_SECONDS // 60} 分钟内有效。如非本人操作请忽略本邮件。\n"
    )

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls(context=context)
            smtp.ehlo()
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


def create_login_code(email: str, request_ip: str | None = None) -> dict[str, Any]:
    email = normalize_email(email)
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_at FROM email_login_codes
                WHERE email = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email,),
            )
            latest = cur.fetchone()
            if latest and latest["created_at"]:
                created = latest["created_at"]
                if isinstance(created, datetime) and (now - created).total_seconds() < CODE_RATE_LIMIT_SECONDS:
                    raise AuthError("验证码发送过于频繁，请稍后再试", status=429)

            code = f"{secrets.randbelow(1_000_000):06d}"
            expires_at = now + timedelta(seconds=CODE_TTL_SECONDS)
            cur.execute(
                """
                INSERT INTO email_login_codes(email, code_hash, expires_at, created_at, request_ip)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (email, hash_secret(code), expires_at, now, (request_ip or "")[:64] or None),
            )

    send_login_email(email, code)
    return {
        "email": email,
        "expires_in": CODE_TTL_SECONDS,
        "delivery": "smtp" if smtp_configured() else "server_log",
    }


def upsert_user(email: str) -> dict[str, Any]:
    email = normalize_email(email)
    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, created_at, last_login_at FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, row["id"]))
                return {
                    "id": int(row["id"]),
                    "email": row["email"],
                    "created_at": row["created_at"].isoformat()
                    if hasattr(row["created_at"], "isoformat")
                    else str(row["created_at"]),
                    "last_login_at": now.isoformat(),
                }
            cur.execute(
                "INSERT INTO users(email, created_at, last_login_at) VALUES (%s, %s, %s)",
                (email, now, now),
            )
            user_id = int(cur.lastrowid)
            return {
                "id": user_id,
                "email": email,
                "created_at": now.isoformat(),
                "last_login_at": now.isoformat(),
            }


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = utc_now()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions(user_id, token_hash, expires_at, created_at, last_seen_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, hash_secret(token), expires_at, now, now),
            )
    return token


def verify_login_code(email: str, code: str) -> tuple[dict[str, Any], str]:
    email = normalize_email(email)
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise AuthError("请输入 6 位数字验证码")

    now = utc_now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code_hash, expires_at, consumed_at, failed_attempts
                FROM email_login_codes
                WHERE email = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email,),
            )
            row = cur.fetchone()
            if not row:
                raise AuthError("验证码无效或已过期")
            if row["consumed_at"] is not None:
                raise AuthError("验证码已使用，请重新获取")
            if row["expires_at"] < now:
                raise AuthError("验证码已过期，请重新获取")
            if int(row.get("failed_attempts") or 0) >= CODE_MAX_ATTEMPTS:
                raise AuthError("验证码尝试次数过多，请重新获取", status=429)
            if row["code_hash"] != hash_secret(code):
                cur.execute(
                    "UPDATE email_login_codes SET failed_attempts = failed_attempts + 1 WHERE id = %s",
                    (row["id"],),
                )
                raise AuthError("验证码不正确")
            cur.execute(
                "UPDATE email_login_codes SET consumed_at = %s WHERE id = %s",
                (now, row["id"]),
            )

    user = upsert_user(email)
    token = create_session(int(user["id"]))
    return user, token


def parse_cookies(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    raw = handler.headers.get("Cookie", "")
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}


def session_cookie_header(token: str, clear: bool = False) -> str:
    secure = "; Secure" if security.cookie_secure() else ""
    if clear:
        return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
    max_age = SESSION_TTL_DAYS * 24 * 60 * 60
    return f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"


def get_user_by_session_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = utc_now()
    token_hash = hash_secret(token)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id AS session_id, s.expires_at, u.id AS user_id, u.email, u.created_at, u.last_login_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                cur.execute("DELETE FROM sessions WHERE id = %s", (row["session_id"],))
                return None
            cur.execute("UPDATE sessions SET last_seen_at = %s WHERE id = %s", (now, row["session_id"]))
            return {
                "id": int(row["user_id"]),
                "email": row["email"],
                "created_at": row["created_at"].isoformat()
                if hasattr(row["created_at"], "isoformat")
                else str(row["created_at"]),
                "last_login_at": row["last_login_at"].isoformat()
                if row["last_login_at"] and hasattr(row["last_login_at"], "isoformat")
                else (str(row["last_login_at"]) if row["last_login_at"] else None),
            }


def current_user(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    cookies = parse_cookies(handler)
    return get_user_by_session_token(cookies.get(SESSION_COOKIE))


def logout_session(handler: BaseHTTPRequestHandler) -> None:
    cookies = parse_cookies(handler)
    token = cookies.get(SESSION_COOKIE)
    if not token:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_secret(token),))
