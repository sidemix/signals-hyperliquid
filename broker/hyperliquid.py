import os, math, httpx, json
from typing import Optional

HL_INFO_URL = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/") + "/info"

def _env_float(key: str, default: Optional[float] = None) -> Optional[float]:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default

def _coin_from_symbol(symbol: str) -> str:
    """
    'ETH/USD' -> 'ETH', 'BTC-USD' -> 'BTC', 'ARBUSD' -> 'ARB'
    """
    s = symbol.upper().replace("-", "/")
    if "/" in s:
        return s.split("/")[0]
    if s.endswith("USD"):
        return s[:-3]
    return s

def _fetch_mid_price(coin: str, timeout_s: float = 4.0) -> Optional[float]:
    """
    Query top-of-book via /info l2Book and return mid price.
    This is reliable and cheap.
    """
    try:
        payload = {"type": "l2Book", "coin": coin}
        r = httpx.post(HL_INFO_URL, json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        # expected shape: {"levels":{"bids":[[price,size],...],"asks":[[price,size],...]}, ...}
        bids = data.get("levels", {}).get("bids") or data.get("bids")
        asks = data.get("levels", {}).get("asks") or data.get("asks")
        if not bids or not asks:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        return (best_bid + best_ask) / 2.0
    except Exception as e:
        print(f"[BROKER] mid price fetch error for {coin}: {e}")
        return None

def _compute_qty(symbol: str, notional_usd: float) -> Optional[float]:
    """
    Convert USD notional to quantity using HL mid price.
    Respects HYPER_FIXED_QTY if provided (overrides price fetch).
    """
    fixed = _env_float("HYPER_FIXED_QTY", None)
    if fixed and fixed > 0:
        return fixed

    coin = _coin_from_symbol(symbol)
    px = _fetch_mid_price(coin)
    if not px or px <= 0:
        return None

    # conservative rounding; adjust per-market if you like
    qty = notional_usd / px
    # round to 6 decimals to be safe (HL accepts many perps at 4–6 dp)
    return float(f"{qty:.6f}")
