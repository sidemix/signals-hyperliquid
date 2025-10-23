# broker/hyperliquid.py
# Compatible broker for multiple hyperliquid SDK variants.
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# --- SDK imports (tolerate version differences) -----------------------------
try:
    # Modern split packages
    from hyperliquid.exchange import Exchange      # type: ignore
    from hyperliquid.info import Info              # type: ignore
except Exception:
    # Older single-namespace builds
    from hyperliquid import Exchange, Info         # type: ignore  # noqa: F401

# Some builds have dataclass order types; many accept dicts too.
try:
    from hyperliquid.utils.signing import OrderType  # type: ignore
except Exception:
    OrderType = None  # type: ignore

# --- ENV --------------------------------------------------------------------
ONLY = {s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()}
DEFAULT_TIF = os.getenv("HYPER_TIF", "PostOnly")
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY")  # Agent wallet private key hex

# --- Small helpers ----------------------------------------------------------
def _round8(x: float) -> float:
    return float(f"{x:.8f}")

def _normalize_resp(resp: Any) -> Any:
    """Convert bytes/bytearray responses to JSON/dict when possible."""
    if isinstance(resp, (bytes, bytearray)):
        try:
            return json.loads(resp.decode())
        except Exception:
            # Some SDKs return b'OK' etc.; just return as-is if not JSON.
            return resp
    return resp

def _order_type_from_tif(tif: str) -> Dict[str, Any]:
    """
    Preferred modern shape uses Limit tif=Gtc/Ioc/Alo (Alo == post-only).
    We emit that first, and let the caller retry with legacy if needed.
    """
    t = (tif or "").strip().lower()
    if t == "postonly":
        return {"limit": {"tif": "Alo"}}
    if t in ("gtc", "ioc", "alo"):
        # Keep correct capitalization expected by many services
        cap = {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}[t]
        return {"limit": {"tif": cap}}
    return {"limit": {"tif": "Gtc"}}

def _legacy_post_only() -> Dict[str, Any]:
    """Legacy post-only order_type some gateways expect."""
    return {"postOnly": {}}

def _should_skip(symbol: str) -> bool:
    if not ONLY:
        return False
    return symbol.upper() not in ONLY

# --- Public signal wire model we receive from executor ----------------------
@dataclass
class ExecSignal:
    side: str                # "LONG" or "SHORT"
    symbol: str              # "BTC/USD" etc.
    entry_low: float
    entry_high: float
    stop_loss: Optional[float]
    leverage: Optional[float]
    tif: Optional[str] = None

# --- Wallet/clients ---------------------------------------------------------
def _mk_clients() -> Tuple[Any, Any]:
    """
    Create Exchange + Info using just the private key. Different SDK builds
    accept different ctor styles; we try common ones.
    """
    if not PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    # Try: Exchange(wallet=<hex>) – modern builds
    last_err = None
    try:
        ex = Exchange(wallet=PRIVKEY)  # type: ignore
        info = Info()
        return ex, info
    except Exception as e:  # noqa: BLE001
        last_err = e

    # Try: Exchange(privkey=<hex>) – older builds
    try:
        ex = Exchange(privkey=PRIVKEY)  # type: ignore
        info = Info()
        return ex, info
    except Exception as e:  # noqa: BLE001
        last_err = e

    raise RuntimeError(f"Could not construct Exchange with any style: {last_err}")

# --- Bulk with robustness ---------------------------------------------------
def _try_bulk_with_rounding(ex: Any, order: Dict[str, Any]) -> Any:
    """
    Call bulk_orders with:
      1) preferred Alo shape
      2) legacy postOnly fallback if server says order type invalid
      3) gentle size nudges to avoid float_to_wire rounding guards
      4) response normalization (bytes -> json)
    """
    order = dict(order)  # copy for mutation
    order["sz"] = float(order["sz"])
    order["limit_px"] = float(order["limit_px"])

    def _bulk(o: Dict[str, Any]) -> Any:
        resp = ex.bulk_orders([o])
        return _normalize_resp(resp)

    # 1) preferred attempt
    try:
        return _bulk(order)
    except Exception as e1:  # noqa: BLE001
        msg1 = str(e1)
        # 2) legacy fallback on order type complaint
        if "Invalid order type" in msg1 or "'postOnly'" in msg1:
            legacy = dict(order)
            legacy["order_type"] = _legacy_post_only()
            try:
                return _bulk(legacy)
            except Exception as e2:  # noqa: BLE001
                last_err = e2
        else:
            last_err = e1

    # 3) nudge size down slightly to dodge rounding traps
    step = 1e-8
    for _ in range(6):
        new_sz = max(0.0, float(order["sz"]) - step)
        if new_sz <= 0.0:
            break
        order["sz"] = _round8(new_sz)
        try:
            return _bulk(order)
        except Exception as e3:  # noqa: BLE001
            last_err = e3

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# --- Main entry -------------------------------------------------------------
def submit_signal(sig: ExecSignal) -> None:
    """
    Submit a single banded entry order as a post-only limit at mid-band.
    """
    symbol = sig.symbol.upper()
    side = sig.side.upper()
    if _should_skip(symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    if sig.entry_low is None or sig.entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    # Exchange + Info
    ex, info = _mk_clients()
    log.info("[BROKER] hyperliquid.py loaded")

    # Build an order near the middle of the band
    coin = symbol.split("/")[0]
    mid_px = (float(sig.entry_low) + float(sig.entry_high)) / 2.0
    px = _round8(mid_px)

    # Notional -> size (use 50 USD default if not supplied elsewhere)
    notional_usd = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
    # Guard against zero price
    sz = _round8(max(1e-8, notional_usd / max(px, 1e-8)))

    # Side/flags
    is_buy = side == "LONG"
    tif = (sig.tif or DEFAULT_TIF or "PostOnly").strip()

    # Order type pref (Alo), fallback handled inside _try_bulk_with_rounding
    order_type = _order_type_from_tif(tif)

    order: Dict[str, Any] = {
        "coin": coin,
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": px,
        "order_type": order_type,
        "reduce_only": False,
    }

    log.info("[BROKER] BUY %s band=(%f,%f) SL=%s lev=%s TIF=%s",
             symbol, float(sig.entry_low), float(sig.entry_high),
             str(sig.stop_loss), str(sig.leverage), tif)
    log.info("[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s reduceOnly=%s",
             "BUY" if is_buy else "SELL", coin, px, sz, tif, False)

    # Place via resilient path
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders response: %s", resp)
