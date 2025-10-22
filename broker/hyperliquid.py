"""
broker/hyperliquid.py
Routes ExecSignal objects to Hyperliquid via REST.
- Computes size from TRADE_SIZE_USD and mark price
- Builds an OTO bracket (entry + stop + TP ladder)
- Honors DRY_RUN
- Logs in a helpful, structured way

REQUIRED ENVs
-------------
HYPERLIQUID_BASE=https://api.hyperliquid.xyz
TRADE_SIZE_USD=100
DRY_RUN=true|false
HYPER_ONLY_EXECUTE_SYMBOLS=BTC/USD,ETH/USD, ...      (optional allow-list)

EVM signing (one of):
- HYPER_EVM_PRIVKEY=0x...                             (preferred; use HyperEVM pk)
or
- HYPER_API_KEY / HYPER_API_SECRET                    (if you have a REST key flow)

NOTE: The final _sign_and_post() stub needs your HL signer.
"""

from __future__ import annotations

import os
import time
import json
import math
import typing as t
import traceback

import httpx

# ---- optional import for type hints only ----
try:
    from execution import ExecSignal   # noqa
except Exception:  # pragma: no cover
    ExecSignal = t.Any  # type: ignore


# =========================
# Config helpers
# =========================
def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


HL_BASE = _env("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz")
HTTP_TIMEOUT = float(os.getenv("HL_HTTP_TIMEOUT", "10"))


# =========================
# Public entry point
# =========================
def submit_signal(sig_or_kwargs: t.Union["ExecSignal", dict], **kw) -> None:
    """
    Called by execution.execute_signal(). Accepts ExecSignal or a dict payload.
    Normalizes into a dict and routes to placement.
    """
    payload = _normalize(sig_or_kwargs, **kw)
    _log_preview(payload)

    if _env_bool("DRY_RUN", False):
        print("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    # minimal safety
    if not (_env("HYPER_EVM_PRIVKEY") or (_env("HYPER_API_KEY") and _env("HYPER_API_SECRET"))):
        raise RuntimeError("HYPER_EVM_PRIVKEY (or API key/secret) not set.")

    # place real order
    _place_order_real(payload)


# =========================
# Normalization / logging
# =========================
def _normalize(sig_or_kwargs: t.Union["ExecSignal", dict], **kw) -> dict:
    if isinstance(sig_or_kwargs, dict):
        d = {**sig_or_kwargs, **kw}
        symbol = d["symbol"]
        side = d["side"]
        band = d.get("entry_band") or (d["entry_low"], d["entry_high"])
        stop = float(d["stop"])
        tps = [float(x) for x in d.get("tps", [])]
        lev = d.get("leverage")
        tf = d.get("timeframe")
    else:
        s = sig_or_kwargs
        symbol, side = s.symbol, s.side
        band = s.entry_band
        stop = float(s.stop)
        tps = list(s.tps)
        lev = s.leverage
        tf = s.timeframe

    low, high = float(band[0]), float(band[1])
    return {
        "symbol": symbol.upper(),
        "side": side.upper(),               # LONG|SHORT
        "entry_band": (low, high),
        "stop": stop,
        "tps": tps,
        "leverage": lev,
        "timeframe": tf,
    }


def _log_preview(p: dict) -> None:
    low, high = p["entry_band"]
    print(
        f"[BROKER] {p['side']} {p['symbol']} "
        f"band=({low:.6f},{high:.6f}) SL={p['stop']:.6f} "
        f"TPn={len(p['tps'])} lev={p.get('leverage') or 'n/a'} TF={p.get('timeframe') or 'n/a'}"
    )


# =========================
# Market / symbol helpers
# =========================
def _split_symbol(sym: str) -> t.Tuple[str, str]:
    """
    'ETH/USD' -> ('ETH', 'USD')
    """
    s = sym.replace("-", "/").upper()
    parts = s.split("/")
    if len(parts) != 2:
        raise ValueError(f"Unsupported symbol: {sym}")
    return parts[0], parts[1]


def _get_mark_price(coin: str) -> float:
    """
    Query a mark/last price to compute order size.
    Tries /info/ticker as primary; fallback to /info with body if needed.
    """
    url1 = f"{HL_BASE.rstrip('/')}/info/ticker"
    url2 = f"{HL_BASE.rstrip('/')}/info"

    # try simple ticker list first
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as s:
            r = s.get(url1)
            r.raise_for_status()
            data = r.json()
            for it in data if isinstance(data, list) else []:
                if (it.get("symbol") or it.get("instId") or "").upper().startswith(coin.upper()):
                    px = it.get("markPrice") or it.get("lastPrice") or it.get("price")
                    if px is not None:
                        return float(px)
    except Exception:
        pass  # fall back

    # flexible /info body
    try:
        body = {"type": "ticker", "coins": [coin]}
        with httpx.Client(timeout=HTTP_TIMEOUT) as s:
            r = s.post(url2, json=body)
            r.raise_for_status()
            data = r.json()
            # try a couple of shapes
            px = None
            if isinstance(data, dict):
                px = (
                    data.get("markPx")
                    or data.get("markPrice")
                    or data.get("lastPx")
                    or data.get("lastPrice")
                )
            if px is None and isinstance(data, list) and data:
                cand = data[0]
                px = cand.get("markPx") or cand.get("markPrice") or cand.get("lastPx") or cand.get("lastPrice")
            if px is not None:
                return float(px)
    except Exception:
        pass

    raise RuntimeError("Could not compute size from mark price; aborting.")


def _compute_size_usd_to_contracts(trade_usd: float, mark_px: float, min_qty: float = 0.0001) -> float:
    raw = trade_usd / max(mark_px, 1e-9)
    # round down to reasonable precision to avoid rejections
    prec = 6
    qty = math.floor(raw * (10 ** prec)) / (10 ** prec)
    return max(qty, min_qty)


# =========================
# OTO / order construction
# =========================
def _place_order_real(p: dict) -> None:
    """
    Build and submit the entry + stop + take profits.
    This function is fully wired except the final signature step
    in _sign_and_post(), which you must complete per HL docs or SDK.
    """
    coin, quote = _split_symbol(p["symbol"])  # ('ETH','USD')
    side = p["side"]                          # LONG | SHORT
    entry_low, entry_high = p["entry_band"]
    stop_px = float(p["stop"])
    tp_list = [float(x) for x in p["tps"]]

    # 1) get mark px and compute size
    trade_usd = float(os.getenv("TRADE_SIZE_USD", "100"))
    mark = _get_mark_price(coin)
    size = _compute_size_usd_to_contracts(trade_usd, mark)

    # 2) choose entry px (mid of band)
    entry_px = (entry_low + entry_high) / 2.0

    is_buy = True if side == "LONG" else False
    reduce_only = False

    # 3) place entry limit
    entry_payload = _build_limit_order(
        coin=coin,
        is_buy=is_buy,
        size=size,
        px=entry_px,
        tif="Gtc",              # Good till cancel
        reduce_only=reduce_only
    )

    _sign_and_post(entry_payload)  # <— implement signing in this helper

    # 4) place stop-loss (reduce only)
    stop_reduce_only = True
    stop_trigger = _build_trigger_order(
        coin=coin,
        is_buy=not is_buy,           # closes the position
        size=size,
        trigger_px=stop_px,
        kind="stop",                 # or "stopMarket" if HL requires
        reduce_only=stop_reduce_only
    )
    _sign_and_post(stop_trigger)

    # 5) place TP ladder (reduce-only)
    for tp_px in tp_list:
        tp_reduce_only = True
        tp_trigger = _build_trigger_order(
            coin=coin,
            is_buy=not is_buy,
            size=round(size / max(len(tp_list), 1), 6),   # split evenly
            trigger_px=tp_px,
            kind="takeProfit",          # or "takeProfitMarket" depending on HL
            reduce_only=tp_reduce_only
        )
        _sign_and_post(tp_trigger)


# -------- payload builders (keep these boring & explicit) --------
def _build_limit_order(
    *,
    coin: str,
    is_buy: bool,
    size: float,
    px: float,
    tif: str = "Gtc",
    reduce_only: bool = False
) -> dict:
    """
    Shape aligns with Hyperliquid's typical 'order' post.
    You may need to adjust outer keys to match HL exactly.
    """
    body = {
        "type": "order",
        "order": {
            "coin": coin,
            "is_buy": bool(is_buy),
            "sz": f"{size:.6f}",
            "limit_px": f"{px:.6f}",
            "order_type": {"limit": {"tif": tif}},
            "reduce_only": bool(reduce_only),
        },
        "timestamp_ms": int(time.time() * 1000),
    }
    return body


def _build_trigger_order(
    *,
    coin: str,
    is_buy: bool,
    size: float,
    trigger_px: float,
    kind: str,              # "stop" | "stopMarket" | "takeProfit" | "takeProfitMarket"
    reduce_only: bool = True
) -> dict:
    """
    Construct a generic trigger-type order payload. Adjust 'kind' naming to HL docs.
    """
    body = {
        "type": "order",
        "order": {
            "coin": coin,
            "is_buy": bool(is_buy),
            "sz": f"{size:.6f}",
            "order_type": {
                "trigger": {
                    "kind": kind,
                    "trigger_px": f"{trigger_px:.6f}",
                    "tif": "Gtc",
                }
            },
            "reduce_only": bool(reduce_only),
        },
        "timestamp_ms": int(time.time() * 1000),
    }
    return body


# =========================
# FINAL POST (SIGN HERE)
# =========================
def _sign_and_post(payload: dict) -> None:
    """
    The ONLY piece you need to finish to go live.

    Hyperliquid expects a signed request to its private endpoint (often /exchange).
    They support EVM-style signing (private key) or an API key scheme.

    Replace this function with the exact signing required by your account setup:
      - EVM (preferred): sign the canonical message and include signature + address
      - or API key/secret: include headers they document

    The rest of the module (size calc, entries, TP/SL) is already wired.
    """
    url = f"{HL_BASE.rstrip('/')}/exchange"

    # Example skeleton for EVM signing (pseudo — fill per HL doc):
    # msg = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    # sig = sign_with_evm_key(msg, os.getenv("HYPER_EVM_PRIVKEY"))
    # headers = {"Content-Type": "application/json"}
    # body = {"signature": sig, "payload": payload, "address": eth_address_from_key(...)}

    # Until you wire this, raise loudly so nothing silently fails:
    raise NotImplementedError(
        "Replace _place_order_real with real HL placement. "
        "Until then, run with DRY_RUN=true."
    )

    # With signing in place, do:
    # with httpx.Client(timeout=HTTP_TIMEOUT) as s:
        # r = s.post(url, json=body, headers=headers)
        # r.raise_for_status()
        # print("[BROKER] POST ok:", r.text)
