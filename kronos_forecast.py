"""Adapter for the locally installed Kronos-small Hyperliquid predictor."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
KRONOS_ROOT = Path(os.environ.get("KRONOS_WORKSPACE", str(DEFAULT_ROOT))).expanduser().resolve()
PYTHON = KRONOS_ROOT / "Kronos" / ".venv" / "bin" / "python"
PREDICTOR = KRONOS_ROOT / "kronos_hyperliquid_predict.py"
MODEL_DIR = KRONOS_ROOT / "Kronos" / "models" / "Kronos-small"
TOKENIZER_DIR = KRONOS_ROOT / "Kronos" / "models" / "Kronos-Tokenizer-base"
XYZ_CONTRACTS = {"SNDK", "NVDA", "MU", "TSLA", "AAPL", "META", "GOOGL", "AMZN"}
VALID_INTERVALS = {"1h", "4h", "1d"}
_PREDICT_LOCK = threading.Lock()
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: dict[str, Any] | None = None
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"


class KronosForecastError(RuntimeError):
    pass


def _utc_timestamp(value: Any) -> str:
    """Mark Kronos' timezone-naive timestamps as UTC and normalize aware values."""
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def status() -> dict[str, Any]:
    ready = all(path.exists() for path in (PYTHON, PREDICTOR, MODEL_DIR, TOKENIZER_DIR))
    return {"ready": ready, "model": "Kronos-small", "source": "Hyperliquid", "read_only": True}


def _post_info(payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        HYPERLIQUID_INFO_URL,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MarketBrief-AI/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise KronosForecastError(f"Hyperliquid 市场目录暂时不可用: {exc}") from exc


def _catalog_items(meta: Any, *, dex: str = "") -> list[dict[str, str]]:
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    items = []
    for row in universe:
        if not isinstance(row, dict) or row.get("isDelisted"):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        bare = name.split(":", 1)[-1].upper()
        value = name if ":" in name else (f"{dex}:{bare}" if dex else bare)
        items.append({"value": value, "label": f"{bare}-USDC", "search": f"{bare} {value}".upper()})
    return items


def contract_catalog(*, force: bool = False) -> dict[str, Any]:
    """Return a catalog refreshed once per MarketBrief process startup."""
    global _CATALOG_CACHE
    with _CATALOG_LOCK:
        if not force and _CATALOG_CACHE:
            return _CATALOG_CACHE
        catalog = {
            "groups": [
                {"key": "perps", "label": "合约", "items": _catalog_items(_post_info({"type": "meta"}))},
                {"key": "tradfi", "label": "传统金融 · XYZ", "items": _catalog_items(_post_info({"type": "meta", "dex": "xyz"}), dex="xyz")},
            ],
            "source": "Hyperliquid",
            "synced_at": int(time.time()),
            "refresh_policy": "process_start",
        }
        _CATALOG_CACHE = catalog
        return catalog


def normalize_contract(value: str) -> str:
    symbol = str(value or "").strip()
    if symbol.upper().endswith("-USDC"):
        symbol = symbol[:-5]
    bare = symbol.upper()
    if not re.fullmatch(r"(?:[A-Za-z0-9._-]+:)?[A-Za-z0-9._-]+", symbol):
        raise KronosForecastError("合约代码格式无效")
    if ":" not in symbol and bare in XYZ_CONTRACTS:
        return f"xyz:{bare}"
    return symbol if ":" in symbol else bare


def predict(payload: dict[str, Any]) -> dict[str, Any]:
    if not status()["ready"]:
        raise KronosForecastError("本地 Kronos 模型或运行环境未就绪")
    symbol = normalize_contract(str(payload.get("symbol") or "BTC"))
    interval = str(payload.get("interval") or "4h").lower()
    if interval not in VALID_INTERVALS:
        raise KronosForecastError("K 线周期仅支持 1h、4h 或 1d")
    try:
        pred_len = max(1, min(int(payload.get("pred_len", 6)), 12))
        samples = max(1, min(int(payload.get("samples", 5)), 20))
    except (TypeError, ValueError) as exc:
        raise KronosForecastError("预测根数或采样路径无效") from exc
    if not _PREDICT_LOCK.acquire(blocking=False):
        raise KronosForecastError("模型正在生成另一组预测，请稍后再试")
    try:
        command = [
            str(PYTHON), str(PREDICTOR), symbol,
            "--interval", interval, "--lookback", "400",
            "--pred-len", str(pred_len), "--samples", str(samples), "--json",
        ]
        completed = subprocess.run(
            command,
            cwd=KRONOS_ROOT,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("KRONOS_TIMEOUT_SECONDS", "300")),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KronosForecastError("预测计算超时，请减少采样路径后重试") from exc
    finally:
        _PREDICT_LOCK.release()
    if completed.returncode != 0:
        detail = (completed.stdout.strip() or completed.stderr.strip()).splitlines()[-1:]
        raise KronosForecastError(detail[0] if detail else "模型预测失败")
    start = completed.stdout.find("{")
    if start < 0:
        raise KronosForecastError("模型没有返回有效结果")
    try:
        result = json.loads(completed.stdout[start:])
    except json.JSONDecodeError as exc:
        raise KronosForecastError("模型返回结果无法解析") from exc
    for candle in result.get("forecast", []):
        if isinstance(candle, dict) and candle.get("timestamp"):
            candle["timestamp"] = _utc_timestamp(candle["timestamp"])
    result["model"] = "Kronos-small"
    result["read_only"] = True
    return result
