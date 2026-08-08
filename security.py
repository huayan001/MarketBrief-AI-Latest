"""Small production-safety helpers shared by the HTTP server and auth layer."""

from __future__ import annotations

import os
import ipaddress
import urllib.parse
from http.server import BaseHTTPRequestHandler


def is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() == "production"


def cookie_secure() -> bool:
    configured = os.environ.get("COOKIE_SECURE", "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return is_production()


def response_headers(content_type: str = "") -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store" if "application/json" in content_type else "no-cache",
    }
    if "text/html" in content_type:
        headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; connect-src 'self'; "
            "font-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
    if is_production():
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def request_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    """Reject browser cross-site writes while preserving CLI and webhook clients."""

    request_host = str(handler.headers.get("Host") or "").strip()
    host_name = urllib.parse.urlparse(f"//{request_host}").hostname or ""
    client_address = getattr(handler, "client_address", ("",))
    client_host = str(client_address[0]) if isinstance(client_address, (tuple, list)) and client_address else ""
    try:
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        client_is_loopback = client_host.lower() == "localhost"
    # The local desktop build is bound to 127.0.0.1 only. Browser wrappers may
    # omit or rewrite Origin/Sec-Fetch-* inconsistently, so loopback socket +
    # loopback Host is the reliable trust boundary in development.
    if (
        not is_production()
        and host_name.lower() in {"localhost", "127.0.0.1", "::1"}
        and client_is_loopback
    ):
        return True

    origin = str(handler.headers.get("Origin") or "").strip()
    if origin.lower() == "null":
        # Some macOS browser wrappers rewrite a same-origin local request to
        # Origin: null. Accept it only when both the socket peer and Host are
        # loopback, while still rejecting cross-site browser requests.
        fetch_site = str(handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return (
            host_name.lower() in {"localhost", "127.0.0.1", "::1"}
            and client_is_loopback
            and fetch_site != "cross-site"
        )
    if not origin:
        fetch_site = str(handler.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return fetch_site not in {"cross-site"}
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    forwarded_host = str(handler.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    request_host = forwarded_host or request_host
    if parsed.netloc == request_host:
        return True
    request_url = urllib.parse.urlparse(f"//{request_host}")
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    return (
        (parsed.hostname or "").lower() in loopback_hosts
        and (request_url.hostname or "").lower() in loopback_hosts
        and parsed.port == request_url.port
    )
