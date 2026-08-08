"""Minimal outbound notification adapters for the paid daily-brief loop."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Any


SUPPORTED_CHANNELS = ("email", "wechat", "feishu", "telegram")


class NotificationError(RuntimeError):
    pass


def channel_status(user_configs: dict[str, Any] | None = None) -> dict[str, bool]:
    user_configs = user_configs or {}
    return {
        "email": bool(os.environ.get("SMTP_HOST", "").strip()),
        "wechat": "wechat" in user_configs,
        "feishu": "feishu" in user_configs,
        "telegram": "telegram" in user_configs,
    }


def _post_json(url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "MarketBriefAI/0.3"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 300:
                raise NotificationError(f"HTTP {response.status}")
            body = response.read().decode("utf-8", errors="replace")
            if body:
                parsed = json.loads(body)
                code = parsed.get("errcode", parsed.get("code", parsed.get("ok", 0)))
                if code not in {0, "0", True, None}:
                    raise NotificationError(str(parsed.get("errmsg") or parsed.get("msg") or parsed)[:300])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise NotificationError(str(exc)) from exc


def send_email(recipient: str, subject: str, text: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise NotificationError("SMTP 未配置")
    port = int(os.environ.get("SMTP_PORT", "465") or "465")
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not sender:
        raise NotificationError("SMTP_FROM 或 SMTP_USER 未配置")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text)
    context = ssl.create_default_context()
    use_ssl = os.environ.get("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    use_tls = os.environ.get("SMTP_USE_TLS", "0").lower() in {"1", "true", "yes"}
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as client:
            if user:
                client.login(user, password)
            client.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=20) as client:
        client.ehlo()
        if use_tls:
            client.starttls(context=context)
            client.ehlo()
        if user:
            client.login(user, password)
        client.send_message(message)


def send(
    channel: str,
    recipient: str,
    subject: str,
    text: str,
    config: dict[str, Any] | None = None,
) -> None:
    config = config or {}
    if channel == "email":
        send_email(recipient, subject, text)
        return
    if channel == "wechat":
        url = str(config.get("webhook_url") or "").strip()
        if not url:
            raise NotificationError("企业微信 Webhook 未配置")
        _post_json(url, {"msgtype": "markdown", "markdown": {"content": f"**{subject}**\n{text}"}})
        return
    if channel == "feishu":
        url = str(config.get("webhook_url") or "").strip()
        if not url:
            raise NotificationError("飞书 Webhook 未配置")
        _post_json(url, {"msg_type": "text", "content": {"text": f"{subject}\n\n{text}"}})
        return
    if channel == "telegram":
        token = str(config.get("bot_token") or "").strip()
        chat_id = str(config.get("chat_id") or "").strip()
        if not token or not chat_id:
            raise NotificationError("Telegram 未配置")
        url = f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/sendMessage"
        _post_json(url, {"chat_id": chat_id, "text": f"{subject}\n\n{text}"[:4096]})
        return
    raise NotificationError(f"不支持的通知渠道: {channel}")


def send_with_retry(
    channel: str,
    recipient: str,
    subject: str,
    text: str,
    *,
    attempts: int = 3,
    config: dict[str, Any] | None = None,
) -> int:
    """Send a notification and return the number of attempts used."""
    attempts = max(1, min(int(attempts), 5))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            send(channel, recipient, subject, text, config=config)
            return attempt
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.25 * (2 ** (attempt - 1)))
    raise NotificationError(f"推送重试 {attempts} 次后失败：{last_error}") from last_error
