"""Read-only Robinhood Chain pool radar backed by the public JSON-RPC endpoint."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from concurrent.futures import ThreadPoolExecutor


DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CHAIN_ID = 4663
EXPLORER_URL = "https://robinhoodchain.blockscout.com"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
V2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"
V3_FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
KNOWN_QUOTES = {WETH: "WETH", USDG: "USDG"}
TOKEN_CALLS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "total_supply": "0x18160ddd",
}


class RobinhoodRadarError(RuntimeError):
    pass


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "MarketBriefAI/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Invalid data response")
    return result


def _number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None and value != "" else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def market_data(asset: str, pool: str) -> dict[str, Any]:
    """Fetch the exact selected pool, never substitute another chain or pool."""
    if not all(re.fullmatch(r"0x[0-9a-fA-F]{40}", value or "") for value in (asset, pool)):
        raise ValueError("无效的代币或交易池地址")
    asset, pool = asset.lower(), pool.lower()
    result: dict[str, Any] = dict.fromkeys(("market_cap", "price_usd", "change_24h", "volume_24h", "liquidity_usd", "holders"))
    sources, notes = [], []
    with ThreadPoolExecutor(max_workers=2) as executor:
        market = executor.submit(_get_json, f"https://api.dexscreener.com/latest/dex/pairs/robinhood/{pool}")
        token = executor.submit(_get_json, f"https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/{asset}/info")
        try:
            pairs = market.result().get("pairs") or []
            pair = next((p for p in pairs if
                         str(p.get("chainId", "")).lower() in {"robinhood", "robinhoodchain", "4663"}
                         and str(p.get("pairAddress", "")).lower() == pool
                         and str((p.get("baseToken") or {}).get("address", "")).lower() == asset), None)
            if pair:
                result.update(market_cap=_number(pair.get("marketCap")), price_usd=_number(pair.get("priceUsd")),
                              change_24h=_number((pair.get("priceChange") or {}).get("h24")),
                              volume_24h=_number((pair.get("volume") or {}).get("h24")),
                              liquidity_usd=_number((pair.get("liquidity") or {}).get("usd")))
                sources.append("DEX Screener（所选池）")
            else:
                notes.append("行情源尚未收录所选池或不支持该报价方向")
        except (OSError, ValueError):
            notes.append("行情源暂时无法连接")
        try:
            data = token.result().get("data") or {}
            attributes = data.get("attributes") or {}
            if str(attributes.get("address", "")).lower() == asset and str(data.get("id", "")).lower() == f"robinhood_{asset}":
                result["holders"] = _number((attributes.get("holders") or {}).get("count"))
                sources.append("GeckoTerminal（持有地址数）")
            else:
                notes.append("持有者数据源尚未收录该代币")
        except (OSError, ValueError):
            notes.append("持有者数据暂时无法读取")
    result.update(asset=asset, pool=pool, sources=sources, notes=notes,
                  checked_at=datetime.now(timezone.utc).isoformat(),
                  status="ok" if all(result[k] is not None for k in ("market_cap", "price_usd", "change_24h", "volume_24h", "liquidity_usd", "holders"))
                  else "partial" if any(v is not None for v in result.values()) else "unavailable")
    return result


def _rpc_url() -> str:
    return os.environ.get("ROBINHOOD_RPC_URL", DEFAULT_RPC_URL).strip() or DEFAULT_RPC_URL


def _post_rpc(payload: Any, *, timeout: float = 12.0) -> Any:
    request = urllib.request.Request(
        _rpc_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MarketBriefAI-RobinhoodRadar/1.0"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RobinhoodRadarError(f"Robinhood Chain 公共 RPC 暂时不可用：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise RobinhoodRadarError(f"Robinhood Chain 公共 RPC 暂时不可用：{exc}") from exc
    raise RobinhoodRadarError("Robinhood Chain 公共 RPC 暂时不可用")


def rpc(method: str, params: list[Any] | None = None) -> Any:
    body = _post_rpc({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    if body.get("error"):
        raise RobinhoodRadarError(str(body["error"].get("message") or body["error"]))
    return body.get("result")


def rpc_batch(calls: list[tuple[str, list[Any]]]) -> list[Any]:
    if not calls:
        return []
    results: list[Any] = []
    # The public endpoint rejects large JSON-RPC batches. Small chunks also keep
    # one busy token launch from starving the rest of the MarketBrief process.
    for start in range(0, len(calls), 12):
        chunk = calls[start : start + 12]
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(chunk)
        ]
        body = _post_rpc(payload)
        if not isinstance(body, list):
            raise RobinhoodRadarError("公共 RPC 未返回批量查询结果")
        by_id = {int(item.get("id")): item for item in body if isinstance(item, dict) and item.get("id") is not None}
        for index in range(len(chunk)):
            item = by_id.get(index, {})
            results.append(None if item.get("error") else item.get("result"))
        if start + len(chunk) < len(calls):
            time.sleep(0.2)
    return results


def _address(word: str) -> str:
    value = str(word or "").lower().removeprefix("0x")
    return "0x" + value[-40:]


def _words(value: str) -> list[str]:
    clean = str(value or "").removeprefix("0x")
    return [clean[index : index + 64] for index in range(0, len(clean), 64) if len(clean[index : index + 64]) == 64]


def _uint(value: str | None) -> int | None:
    try:
        return int(str(value or "0x0"), 16)
    except ValueError:
        return None


def _abi_string(value: str | None) -> str:
    clean = str(value or "").removeprefix("0x")
    if not clean:
        return ""
    try:
        raw = bytes.fromhex(clean)
        if len(raw) >= 64:
            offset = int.from_bytes(raw[:32], "big")
            if offset + 32 <= len(raw):
                length = int.from_bytes(raw[offset : offset + 32], "big")
                if 0 <= length <= len(raw) - offset - 32:
                    return raw[offset + 32 : offset + 32 + length].decode("utf-8", errors="replace").strip()
        return raw[:32].rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    except (ValueError, UnicodeDecodeError):
        return ""


def _decode_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log in logs:
        topics = [str(item).lower() for item in log.get("topics") or []]
        if len(topics) < 3:
            continue
        data_words = _words(log.get("data") or "")
        if topics[0] == PAIR_CREATED_TOPIC and data_words:
            pool = _address(data_words[0])
            venue = "Uniswap V2"
            fee = 0.3
        elif topics[0] == POOL_CREATED_TOPIC and len(data_words) >= 2:
            pool = _address(data_words[1])
            venue = "Uniswap V3"
            fee = (_uint(topics[3]) or 0) / 10_000 if len(topics) > 3 else None
        else:
            continue
        events.append(
            {
                "venue": venue,
                "pool": pool,
                "token0": _address(topics[1]),
                "token1": _address(topics[2]),
                "fee_pct": fee,
                "block_number": _uint(log.get("blockNumber")) or 0,
                "tx_hash": str(log.get("transactionHash") or ""),
            }
        )
    events.sort(key=lambda item: item["block_number"], reverse=True)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event["pool"] in seen:
            continue
        seen.add(event["pool"])
        unique.append(event)
    return unique


def _token_metadata(addresses: set[str]) -> dict[str, dict[str, Any]]:
    ordered = sorted(addresses)
    calls: list[tuple[str, list[Any]]] = []
    labels: list[tuple[str, str]] = []
    for address in ordered:
        for field, selector in TOKEN_CALLS.items():
            calls.append(("eth_call", [{"to": address, "data": selector}, "latest"]))
            labels.append((address, field))
        calls.append(("eth_getCode", [address, "latest"]))
        labels.append((address, "code"))
    values = rpc_batch(calls)
    metadata = {address: {"address": address} for address in ordered}
    for (address, field), value in zip(labels, values):
        if field in {"name", "symbol"}:
            metadata[address][field] = _abi_string(value)
        elif field in {"decimals", "total_supply"}:
            metadata[address][field] = _uint(value)
        else:
            metadata[address]["code_bytes"] = max(0, (len(str(value or "0x")) - 2) // 2)
    for address, symbol in KNOWN_QUOTES.items():
        if address in metadata:
            metadata[address]["symbol"] = symbol
    return metadata


def _block_times(block_numbers: set[int]) -> dict[int, int]:
    ordered = sorted(block_numbers)
    values = rpc_batch([("eth_getBlockByNumber", [hex(number), False]) for number in ordered])
    return {
        number: (_uint((value or {}).get("timestamp")) or 0)
        for number, value in zip(ordered, values)
    }


def _v2_reserves(events: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    v2 = [event for event in events if event["venue"] == "Uniswap V2"]
    values = rpc_batch([("eth_call", [{"to": event["pool"], "data": "0x0902f1ac"}, "latest"]) for event in v2])
    reserves: dict[str, tuple[int, int]] = {}
    for event, value in zip(v2, values):
        words = _words(value or "")
        if len(words) >= 2:
            reserves[event["pool"]] = (int(words[0], 16), int(words[1], 16))
    return reserves


def _friendly_supply(meta: dict[str, Any]) -> float | None:
    supply = meta.get("total_supply")
    decimals = meta.get("decimals")
    if supply is None or decimals is None or decimals > 36:
        return None
    return supply / (10 ** decimals)


def _enrich_event(
    event: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
    block_times: dict[int, int],
    reserves: dict[str, tuple[int, int]],
    now_ts: int,
) -> dict[str, Any]:
    token0 = event["token0"]
    token1 = event["token1"]
    if token0 in KNOWN_QUOTES and token1 not in KNOWN_QUOTES:
        asset, quote, quote_index = token1, token0, 0
    else:
        asset, quote, quote_index = token0, token1, 1
    asset_meta = metadata.get(asset, {"address": asset})
    quote_meta = metadata.get(quote, {"address": quote})
    quote_symbol = KNOWN_QUOTES.get(quote) or quote_meta.get("symbol") or "未知资产"
    created_ts = block_times.get(event["block_number"], 0)
    age_minutes = max(0, (now_ts - created_ts) // 60) if created_ts else None
    flags = ["代币权限尚未审计"]
    score = 35
    if asset_meta.get("symbol") and asset_meta.get("name"):
        score += 15
    else:
        flags.append("元数据不完整")
    if (asset_meta.get("code_bytes") or 0) >= 100:
        score += 10
    else:
        flags.append("合约字节码异常")
    trusted_quote = quote in KNOWN_QUOTES
    if trusted_quote:
        score += 15
    else:
        flags.append("非标准报价资产")

    liquidity_value = None
    reserve_pair = reserves.get(event["pool"])
    if reserve_pair:
        quote_decimals = quote_meta.get("decimals")
        if quote_decimals is not None and quote_decimals <= 36:
            quote_reserve = reserve_pair[quote_index] / (10 ** quote_decimals)
            liquidity_value = quote_reserve * 2
            healthy = (quote == WETH and liquidity_value >= 6) or (quote == USDG and liquidity_value >= 10_000)
            if healthy:
                score += 20
            else:
                score += 5
                flags.append("可读流动性较薄")
    elif event["venue"] == "Uniswap V3":
        flags.append("集中流动性待报价")
    else:
        flags.append("暂未读到池内储备")
    if age_minutes is not None and age_minutes >= 10:
        score += 5

    severe = {"合约字节码异常", "非标准报价资产", "可读流动性较薄"}
    risk_level = "high" if severe.intersection(flags) else "review"
    return {
        **event,
        "asset": asset,
        "symbol": asset_meta.get("symbol") or f"{asset[:6]}…{asset[-4:]}",
        "name": asset_meta.get("name") or "未识别代币",
        "quote": quote,
        "quote_symbol": quote_symbol,
        "total_supply": _friendly_supply(asset_meta),
        "code_bytes": asset_meta.get("code_bytes"),
        "created_at": datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat() if created_ts else None,
        "age_minutes": age_minutes,
        "liquidity_value": round(liquidity_value, 6) if liquidity_value is not None else None,
        "screening_score": min(score, 90),
        "risk_level": risk_level,
        "flags": flags,
        "explorer_url": f"{EXPLORER_URL}/token/{asset}",
        "pool_url": f"{EXPLORER_URL}/address/{event['pool']}",
        "tx_url": f"{EXPLORER_URL}/tx/{event['tx_hash']}",
    }


def scan(*, block_window: int = 12_000, limit: int = 12, force: bool = False) -> dict[str, Any]:
    block_window = max(1_000, min(int(block_window), 100_000))
    limit = max(1, min(int(limit), 50))
    now = time.time()
    with _cache_lock:
        cached = _cache.get("payload")
        if not force and cached and float(_cache.get("expires_at") or 0) > now:
            return {**cached, "cached": True}

    head = _uint(rpc("eth_blockNumber"))
    if head is None:
        raise RobinhoodRadarError("无法读取 Robinhood Chain 最新区块")
    from_block = max(0, head - block_window)
    logs = rpc(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": "latest",
                "address": [V2_FACTORY, V3_FACTORY],
                "topics": [[PAIR_CREATED_TOPIC, POOL_CREATED_TOPIC]],
            }
        ],
    ) or []
    events = _decode_events(logs)[:limit]
    addresses = {address for event in events for address in (event["token0"], event["token1"])}
    metadata = _token_metadata(addresses)
    block_times = _block_times({event["block_number"] for event in events})
    reserves = _v2_reserves(events)
    now_ts = int(time.time())
    pools = [_enrich_event(event, metadata, block_times, reserves, now_ts) for event in events]
    payload = {
        "chain": {
            "name": "Robinhood Chain",
            "chain_id": CHAIN_ID,
            "head_block": head,
            "rpc_mode": "custom" if _rpc_url() != DEFAULT_RPC_URL else "public",
            "rpc_host": _rpc_url().split("//", 1)[-1].split("/", 1)[0],
            "status": "online",
        },
        "scan": {
            "from_block": from_block,
            "to_block": head,
            "block_window": block_window,
            "pool_count": len(pools),
            "review_count": sum(item["risk_level"] == "review" for item in pools),
            "high_risk_count": sum(item["risk_level"] == "high" for item in pools),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "pools": pools,
        "methodology": {
            "coverage": "Uniswap V2/V3 新池事件",
            "missing": "V4、发币平台内部曲线、完整权限与关联钱包审计尚未纳入",
            "meaning": "初筛分代表数据完整度与基础可交易性，不代表上涨概率或安全认证",
        },
        "cached": False,
    }
    with _cache_lock:
        _cache["payload"] = payload
        _cache["expires_at"] = now + 30
    return payload
