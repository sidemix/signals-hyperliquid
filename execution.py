import os
import re
import time
from typing import List, Tuple, Optional
import httpx

# ---------- ENV ----------
HYPER_BASE = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/")
ACCOUNT_MODE = (os.getenv("ACCOUNT_MODE", "perp") or "perp").lower()  # "perp" or "spot"
DRY_RUN = str(os.getenv("DRY_RUN", "false")).lower() in ("1", "true", "yes", "on")
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "100"))
ENTRY_TIMEOUT_MIN = int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))

# Optional allow list: "BTC/USD,ETH/USD" etc.
_ALLOWED_RAW = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").strip()

# TP bucket weights (not used yet for OTO entry-only)
TP_WEIGHTS = [float(x) for x in re.findall(r"[\d.]+", os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20"))]
TP_WEIGHTS = [x for x in TP_WEIGHTS if x > 0]


# ---------- Helpers ----------
def _norm_pair(s: str) -> str:
    """'ETH/USD' or 'eth/usd' -> 'ETH-USD' (default USD if only base is given)."""
    s = s.strip().upper().replace("USDT", "USD")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}-{quote}"
    if "-" in s:
        return s.upper()
    return f"{s}-USD"


def _allowed_set() -> set:
    if not _ALLOWED_RAW:
        return set()
    normd = {_norm_pair(x) for x in _ALLOWED_RAW.split(",") if x.strip()}
    return {x for x in normd if x.endswith("-USD")}


def is_symbol_allowed(symbol: str) -> bool:
    allow = _allowed_set()
    if not allow:
        return True
    return _norm_pair(symbol) in allow


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- Minimal broker calls ----------
def _place_limit(side: str, pair: str, px: float, sz_usd: float) -> Optional[str]:
    """Place a single limit order. Return order id or None."""
    if DRY_RUN:
        print(f"[DRY] place {side} {pair} limit {px} size_usd={sz_usd}")
        return f"dry-{_now_ms()}"
    try:
        payload = {
            "symbol": pair,                 # e.g., ETH-USD
            "side": side.lower(),           # 'buy'|'sell'
            "type": "limit",
            "sizeUsd": round(sz_usd, 2),
            "price": float(px),
            "timeInForce": "gtc",
            "account": ACCOUNT_MODE,        # 'perp'|'spot'
        }
        r = httpx.post(f"{HYPER_BASE}/order", json=payload, timeout=15)
        r.raise_for_status()
        js = r.json()
        return str(js.get("orderId") or js.get("id") or js.get("result"))
    except Exception as e:
        print(f"[ERR] place_limit: {e}")
        return None


# ---------- Signal execution ----------
class ExecSignal:
    def __init__(
        self,
        symbol: str,
        side: str,
        entry_band: Tuple[float, float],
        stop: float,
        tps: List[float],
    ):
        self.symbol = symbol                     # 'ETH/USD'
        self.side = side.upper()                 # 'LONG'|'SHORT'
        self.entry_band = (float(min(entry_band)), float(max(entry_band)))
        self.stop = float(stop)
        self.tps = [float(x) for x in tps]


def execute_signal(sig: ExecSignal) -> str:
    """Place one limit order at the midpoint of the entry band (safe OTO)."""
    pair = _norm_pair(sig.symbol)               # -> ETH-USD
    if not is_symbol_allowed(pair):
        return f"skip ({pair} not allowed)"

    side = "sell" if sig.side == "SHORT" else "buy"
    entry_px = round((sig.entry_band[0] + sig.entry_band[1]) / 2.0, 6)

    oid = _place_limit(side=side, pair=pair, px=entry_px, sz_usd=TRADE_SIZE_USD)
    if oid is None:
        return "order_rejected"

    if ENTRY_TIMEOUT_MIN > 0 and not DRY_RUN:
        print(f"[INFO] placed {oid} on {pair} at {entry_px}. Timeout {ENTRY_TIMEOUT_MIN}m.")
    else:
        print(f"[INFO] placed {oid} on {pair} at {entry_px} (no timeout).")
    return "ok"


__all__ = ["ExecSignal", "execute_signal", "is_symbol_allowed"]
