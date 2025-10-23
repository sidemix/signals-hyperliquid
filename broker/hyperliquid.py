# broker/hyperliquid.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional, Tuple

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# ---- Environment flags / config ----
ONLY_EXECUTE = {
    s.strip().upper().replace("USDT", "USD")
    for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",")
    if s.strip()
}
DRY_RUN = os.getenv("HYPER_DRY_RUN", "true").strip().lower() in {"1", "true", "yes", "y"}

API_KEY = os.getenv("HYPER_API_KEY", "").strip() or None
API_SECRET = os.getenv("HYPER_API_SECRET", "").strip() or None
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY", "").strip() or None
VAULT_ADDRESS = os.getenv("HYPER_VAULT_ADDRESS", "").strip() or None

# Sizing: default target notional in USD for a single order
DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "500"))

# TIF for logs only (SDK dict uses it inside order_type)
DEFAULT_TIF = os.getenv("HYPER_TIF", "Gtc").strip() or "Gtc"
POST_ONLY = os.getenv("HYPER_POST_ONLY", "true").strip().lower() in {"1", "true", "yes", "y"}


def _mk_clients() -> Tuple[Exchange, Info]:
    if API_KEY and API_SECRET:
        ex = Exchange(api_key=API_KEY, secret=API_SECRET)
        info = Info()
        return ex, info
    if PRIVKEY:
        ex = Exchange(agent=PRIVKEY)
        info = Info()
        return ex, info
    raise RuntimeError("No Hyperliquid credentials found. Set HYPER_API_KEY/SECRET or HYPER_PRIVATE_KEY.")


def _normalize_symbol(sym: str) -> str:
    s = (sym or "").upper().replace("USDT", "USD")
    if "/" not in s and s.endswith("USD"):
        s = s[:-3] + "/USD"
    return s


def _coin_from_symbol(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    return symbol.split("/")[0].upper()


def _is_buy(side: str) -> bool:
    s = (side or "").upper()
    if s in {"LONG", "BUY", "BULL", "OPEN_LONG"}:
        return True
    if s in {"SHORT", "SELL", "BEAR", "OPEN_SHORT"}:
        return False
    raise ValueError(f"Unknown side: {side!r}")


def _get_band(sig: Any) -> Tuple[float, float]:
    """
    Accept either sig.entry_band (tuple/list) OR explicit sig.entry_low/sig.entry_high.
    Also tolerates old aliases band_low/band_high or low/high.
    """
    lo = hi = None
    if hasattr(sig, "entry_band"):
        band = getattr(sig, "entry_band")
        if band and isinstance(band, (tuple, list)) and len(band) >= 2:
            lo, hi = band[0], band[1]

    if lo is None:
        lo = getattr(sig, "entry_low", None) or getattr(sig, "band_low", None) or getattr(sig, "low", None)
    if hi is None:
        hi = getattr(sig, "entry_high", None) or getattr(sig, "band_high", None) or getattr(sig, "high", None)

    if lo is None or hi is None:
        raise ValueError("Signal missing entry band; need entry_low/entry_high or entry_band=(low, high).")

    return float(lo), float(hi)


def _mark_price(info: Info, symbol: str) -> Optional[float]:
    try:
        mids = info.all_mids()
        coin = _coin_from_symbol(symbol)
        px = mids.get(coin)
        return float(px) if px is not None else None
    except Exception as e:
        log.warning("Failed to fetch mark price for %s: %s", symbol, e)
        return None


def _size_from_notional(mark_px: float, notional_usd: float) -> float:
    if mark_px <= 0:
        raise ValueError("Invalid mark price")
    return max(notional_usd / mark_px, 0.0)


def _build_order_request(sig: Any, info: Info) -> Dict[str, Any]:
    """
    Build an order request dict compatible with Exchange.bulk_orders([...]).
    This avoids importing OrderType and works across SDK variants.

    Expected format per SDK:
      {
        "coin": "BTC",
        "is_buy": True,
        "sz": "0.001",
        "limit_px": "30000",
        "order_type": {"limit": {"tif": "Gtc", "post_only": True}},
        "reduce_only": False,
        # "cloid": "...",  # optional
      }
    """
    symbol = _normalize_symbol(getattr(sig, "symbol", ""))
    is_buy = _is_buy(getattr(sig, "side", ""))
    entry_lo, entry_hi = _get_band(sig)

    # choose price edge: LONG -> lower edge; SHORT -> upper edge
    px = float(entry_lo if is_buy else entry_hi)

    mark = _mark_price(info, symbol)
    if mark is None:
        mark = px
    sz = _size_from_notional(mark, DEFAULT_NOTIONAL)

    coin = _coin_from_symbol(symbol)
    order_type = {"limit": {"tif": DEFAULT_TIF}}
    if POST_ONLY:
        order_type["limit"]["post_only"] = True

    req: Dict[str, Any] = {
        "coin": coin,
        "is_buy": bool(is_buy),
        "sz": f"{sz:.8f}",
        "limit_px": f"{px:.8f}",
        "order_type": order_type,
        "reduce_only": False,
    }
    return req


def submit_signal(sig: Any) -> None:
    """
    Entry from execution layer.
    """
    log.info("[BROKER] hyperliquid.py loaded, version=hl-broker-compat-2.1")

    symbol = _normalize_symbol(getattr(sig, "symbol", ""))
    if ONLY_EXECUTE:
        log.info("[BROKER] symbol=%s allowed=%s", symbol, ",".join(sorted(ONLY_EXECUTE)))
        if symbol not in ONLY_EXECUTE:
            log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
            return

    entry_lo, entry_hi = _get_band(sig)
    sl = getattr(sig, "stop_loss", None)
    lev = getattr(sig, "leverage", None)
    log.info(
        "[BROKER] %s %s band=(%.6f,%.6f) SL=%s lev=%s TIF=%s",
        (getattr(sig, "side", "") or "").upper(),
        symbol,
        entry_lo,
        entry_hi,
        (f"{sl:.6f}" if isinstance(sl, (int, float)) else "None"),
        (f"{lev:.1f}" if isinstance(lev, (int, float)) else "None"),
        DEFAULT_TIF,
    )

    ex, info = _mk_clients()
    req = _build_order_request(sig, info)

    # Pretty plan log
    log.info(
        "[PLAN] side=%s coin=%s px=%s sz=%s tif=%s reduceOnly=%s",
        ("BUY" if req["is_buy"] else "SELL"),
        req["coin"],
        req["limit_px"],
        req["sz"],
        DEFAULT_TIF,
        req.get("reduce_only", False),
    )

    if DRY_RUN:
        log.info(
            "[DRYRUN] submit LIMIT %s %s px=%s sz=%s tif=%s",
            ("BUY" if req["is_buy"] else "SELL"),
            req["coin"],
            req["limit_px"],
            req["sz"],
            DEFAULT_TIF,
        )
        return

    try:
        resp = ex.bulk_orders([req])
        log.info("[LIVE] order response: %s", resp)
    except Exception as e:
        log.exception("Order placement failed: %s", e)
        raise
