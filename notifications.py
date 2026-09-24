"""Minimal outbound notification adapters for the paid daily-brief loop."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
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


def normalize_outbound_text(value: str) -> str:
    """Make notification text safe for ASCII SMTP transports.

    Yahoo headlines and formatted prices often contain U+00A0 (NBSP) and
    other Unicode spaces. ``smtplib`` encodes a text payload with the ascii
    codec, which raises ``UnicodeEncodeError`` on those characters. Map every
    Unicode space separator to a normal space and drop a leading BOM. Other
    non-ASCII (Chinese, etc.) is preserved and MIME-encoded as UTF-8 later.
    """
    chars: list[str] = []
    for char in str(value or ""):
        if char == "\ufeff":
            continue
        if unicodedata.category(char) == "Zs":
            chars.append(" ")
        else:
            chars.append(char)
    return "".join(chars)


def _single_line(value: str) -> str:
    return " ".join(normalize_outbound_text(value).splitlines()).strip()


def bare_addresses(value: str) -> list[str]:
    found = [addr for _, addr in getaddresses([str(value or "")]) if addr]
    return found or ([str(value).strip()] if str(value or "").strip() else [])


def build_email_message(sender: str, recipient: str, subject: str, text: str) -> EmailMessage:
    """Build a 7-bit UTF-8 message whose serialized form is pure ASCII.

    Quoted-printable/base64 body plus RFC 2047 headers means ``str.encode('ascii')``
    — the call inside ``smtplib.SMTP.sendmail`` — cannot crash on NBSP or CJK.
    """
    policy = SMTP.clone(cte_type="7bit", utf8=False)
    message = EmailMessage(policy=policy)
    message["Subject"] = _single_line(subject)
    message["From"] = _single_line(sender)
    message["To"] = _single_line(recipient)
    message.set_content(normalize_outbound_text(text), subtype="plain", charset="utf-8")
    return message


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
    # Copied app passwords often include NBSP; SMTP AUTH encodes them as ASCII.
    password = "".join(str(os.environ.get("SMTP_PASSWORD", "") or "").split())
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not sender:
        raise NotificationError("SMTP_FROM 或 SMTP_USER 未配置")
    message = build_email_message(sender, recipient, subject, text)
    # Bytes are already 7-bit ASCII. Passing a str into sendmail() would
    # encode with ascii and crash on NBSP (\xa0) or any other non-ASCII.
    payload = message.as_bytes(policy=message.policy)
    envelope_from = bare_addresses(str(message["From"]))
    envelope_to = bare_addresses(str(message["To"]))
    if not envelope_from or not envelope_to:
        raise NotificationError("SMTP 发件人或收件人地址无效")
    context = ssl.create_default_context()
    use_ssl = os.environ.get("SMTP_USE_SSL", "1").lower() not in {"0", "false", "no"}
    use_tls = os.environ.get("SMTP_USE_TLS", "0").lower() in {"1", "true", "yes"}
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as client:
            if user:
                client.login(user, password)
            client.sendmail(envelope_from[0], envelope_to, payload)
        return
    with smtplib.SMTP(host, port, timeout=20) as client:
        client.ehlo()
        if use_tls:
            client.starttls(context=context)
            client.ehlo()
        if user:
            client.login(user, password)
        client.sendmail(envelope_from[0], envelope_to, payload)


def send(
    channel: str,
    recipient: str,
    subject: str,
    text: str,
    config: dict[str, Any] | None = None,
) -> None:
    config = config or {}
    subject = normalize_outbound_text(subject)
    text = normalize_outbound_text(text)
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
