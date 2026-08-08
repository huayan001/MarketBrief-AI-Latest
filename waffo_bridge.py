"""Local bridge between the Python app and the Waffo Node SDK sidecar."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SIDECAR_SCRIPT = ROOT / "waffo" / "service.mjs"
SDK_PACKAGE = ROOT / "node_modules" / "@waffo" / "pancake-ts"

_lock = threading.RLock()
_process: subprocess.Popen[str] | None = None
_base_url: str | None = None
_log_thread: threading.Thread | None = None


class WaffoBridgeError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _node_binary() -> str:
    binary = shutil.which("node")
    if not binary:
        raise WaffoBridgeError("Waffo 支付需要 Node.js 20+，当前未找到 node", 503)
    try:
        version = subprocess.run(
            [binary, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        major = int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise WaffoBridgeError("无法确认 Node.js 版本", 503) from exc
    if major < 20:
        raise WaffoBridgeError(f"Waffo 支付需要 Node.js 20+，当前为 {version}", 503)
    return binary


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _drain_logs(process: subprocess.Popen[str]) -> None:
    if not process.stdout:
        return
    for line in process.stdout:
        text = line.rstrip()
        if text:
            print(f"[waffo] {text}")


def _decode_response(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WaffoBridgeError("Waffo sidecar 返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise WaffoBridgeError("Waffo sidecar 响应必须是 JSON object")
    return payload


def _request(
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    if not _base_url:
        raise WaffoBridgeError("Waffo sidecar 尚未启动", 503)
    request = urllib.request.Request(
        f"{_base_url}{path}",
        data=body,
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _decode_response(response.read())
    except urllib.error.HTTPError as exc:
        payload = _decode_response(exc.read())
        raise WaffoBridgeError(str(payload.get("error") or "Waffo sidecar 请求失败"), exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WaffoBridgeError("无法连接本地 Waffo sidecar", 503) from exc


def start_sidecar(timeout: float = 10) -> None:
    global _base_url, _log_thread, _process
    with _lock:
        if _process and _process.poll() is None and _base_url:
            return
        if not SIDECAR_SCRIPT.is_file() or not SDK_PACKAGE.is_dir():
            raise WaffoBridgeError("缺少 Waffo SDK，请先运行 npm install", 503)

        port = _free_port()
        process = subprocess.Popen(
            [_node_binary(), str(SIDECAR_SCRIPT), str(port)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _process = process
        _base_url = f"http://127.0.0.1:{port}"
        _log_thread = threading.Thread(target=_drain_logs, args=(process,), daemon=True)
        _log_thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _base_url = None
                raise WaffoBridgeError("Waffo sidecar 启动失败，请查看服务端日志", 503)
            try:
                health = _request("/health", timeout=0.5)
                if health.get("ok"):
                    return
            except WaffoBridgeError:
                time.sleep(0.05)

        stop_sidecar()
        raise WaffoBridgeError("等待 Waffo sidecar 启动超时", 503)


def stop_sidecar() -> None:
    global _base_url, _process
    with _lock:
        process = _process
        _process = None
        _base_url = None
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def health() -> dict[str, Any]:
    return _request("/health", timeout=2)


def create_checkout(payload: dict[str, Any]) -> dict[str, Any]:
    return _request(
        "/internal/checkout",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )


def cancel_subscription(order_id: str) -> dict[str, Any]:
    return _request(
        "/internal/subscription/cancel",
        json.dumps({"orderId": order_id}, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )


def lookup_subscription_order(external_id: str) -> dict[str, Any] | None:
    payload = _request(
        "/internal/subscription/lookup",
        json.dumps({"externalId": external_id}, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json; charset=utf-8"},
        timeout=15,
    )
    order = payload.get("order")
    if order is not None and not isinstance(order, dict):
        raise WaffoBridgeError("Waffo 订阅对账响应无效")
    return order


def verify_webhook(raw_body: bytes, signature: str | None) -> dict[str, Any]:
    if not signature:
        raise WaffoBridgeError("缺少 X-Waffo-Signature", 401)
    payload = _request(
        "/internal/verify-webhook",
        raw_body,
        {
            "Content-Type": "application/json",
            "X-Waffo-Signature": signature,
        },
        timeout=5,
    )
    event = payload.get("event")
    if not isinstance(event, dict):
        raise WaffoBridgeError("Waffo webhook 响应缺少 event")
    return event
