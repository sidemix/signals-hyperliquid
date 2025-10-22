# broker/hyperliquid.py
# hl-broker-1.3.0
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hyperliquid.exchange import Exchange  # type: ignore
from hyperliquid.info import Info  # type: ignore
from hyperliquid.utils.signing import OrderType, TIF  # type: ignore

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ---------- Environment ----------
API_KEY = os.getenv("HYPER_API_KEY", "").strip()
API_SECRET = os.getenv("HYPER_API_SECRET", "").strip()

# agent (API wallet) private key (hex string with 0x prefix)
AGENT_PRIVKEY = os.getenv("HYPER_AGENT_PRIVATE_KEY", "").strip() or os.getenv("HYPER_EVM_PRIVKEY", "").strip()

NETWORK = (os.getenv("HYPER_NETWORK", "mainnet") or "mainnet").lower()  # mainnet / testnet
TIF_ENV = os.getenv("HYPER_TIF", "Gtc").strip() or "Gtc"

NOTIONAL_USD = float(os.getenv("HYPER_NOTIONAL_USD", "0") or "0")
FIXED_QTY = float(os.getenv("HYPER_FIXED_QTY", "0") or "0")

ONLY_EXECUTE = [s.strip() for s in (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "") or "").split(",") if s.strip()]

# --------- Helpers ---------
def _coin_from_symbol(symbol: str) -> str:
    # "ETH/USD" -> "ETH"
    if "/" in symbol:
        return symbol.split("/")[0].strip()
    return symbol.strip()

_EX: Optional[Exchange] = None
_INFO: Optional[Info] = None

def _get_exchange() -> Exchange:
    global _EX
    if _EX is None:
        if not AGENT_PRIVKEY:
            raise RuntimeError("HYPER_AGENT_PRIVATE_KEY / HYPER_EVM_PRIVKEY missing")
        _EX = Exchange(AGENT_PRIVKEY, network=NETWORK, api_key=API_KEY or None, api_secret=API_SECRET or None)
    return _EX

def _get_info() -> Info:
    global _INFO
    if _INFO is None:
        _INFO = Info(network=NETWORK)
    return _INFO

# ---------- Price ----------
def _get_mark_price(coin: str) -> Optional[float]:
    """
    Best-effort mark fetch. Never raises, never blocks the flow.
    Returns None on any issue; caller will fall back to entry mid.
    """
    try:
        info = _get_info()
        # 1) preferred per-coin accessor
        if hasattr(info, "mid_price"):
            px = info.mid_price(coin)  # type: ignore[attr-defined]
            if px is not None:
                pxf = float(px)
                log.info(f"[PRICE] {coin} mark={pxf}")
                return pxf
        # 2) fall back to all mids dict
        if hasattr(info, "all_mids"):
            mids = info.all_mids()  # type: ignore[attr-defined]
            if isinstance(mids, dict) and coin in mids and mids[coin] is not None:
                pxf = float(mids[coin])
                log.info(f"[PRICE] {coin} mark={pxf}")
                return pxf
    except Exception as e:
        log.warning(f"[PRICE] {coin} mark fetch failed ({e}); will size off entry band")
    return None

# ---------- Order Build ----------
@dataclass
class Plan:
    is_buy: bool
    coin: str
    px_str: str
    sz_str: str
    tif: str
    reduce_only: bool = False

def _round_price_for(coin: str, px: float) -> str:
    # conservative tick handling: 4 dp for majors, more for small caps
    if coin in ("BTC", "ETH", "BNB", "SOL", "LINK", "AVAX"):
        return f"{px:.1f}" if coin == "BTC" else f"{px:.4f}"
    return f"{px:.6f}"

def _round_size(sz: float) -> str:
    # 8dp is safe for SDK
    return f"{sz:.8f}"

def _make_order_type(tif: str) -> OrderType:
    tif_key = (tif or "Gtc").lower()
    tif_enum = {
        "gtc": TIF.Gtc,
        "ioc": TIF.Ioc,
        "fok": TIF.Fok,
    }.get(tif_key, TIF.Gtc)
    return OrderType.Limit(tif_enum)

def _build_order_plan(
    side: str,
    symbol: str,
    band_low: float,
    band_high: float,
    stop_loss: float,
    leverage: float,
    tif: str,
) -> Plan:
    coin = _coin_from_symbol(symbol)
    is_buy = side.upper().startswith("L")

    log.info(
        f"[BUILD] side={'BUY' if is_buy else 'SELL'} symbol={symbol} coin={coin} "
        f"band=({band_low},{band_high}) SL={stop_loss} lev={leverage} tif={tif}"
    )

    # Entry reference price: mid of entry band
    px_entry = (band_low + band_high) / 2.0
    mark = _get_mark_price(coin)

    ref_px = mark if isinstance(mark, (int, float)) and mark > 0 else px_entry
    if mark is None:
        log.info(f"[PRICE] {coin} mark unavailable; sizing from entry mid {px_entry}")

    # Size preference: NOTIONAL_USD -> FIXED_QTY
    if NOTIONAL_USD > 0:
        sz_val = max(NOTIONAL_USD / ref_px, 1e-8)
    else:
        sz_val = max(FIXED_QTY, 1e-8)

    px_str = _round_price_for(coin, px_entry)
    sz_str = _round_size(sz_val)

    log.info(
        f"[PLAN] side={'BUY' if is_buy else 'SELL'} coin={coin} px={px_str} sz={sz_str} "
        f"tif={tif} reduceOnly=False"
    )
    return Plan(is_buy=is_buy, coin=coin, px_str=px_str, sz_str=sz_str, tif=tif or "Gtc", reduce_only=False)

# ---------- Submit ----------
def _place_order_real(*, is_buy: bool, coin: str, px_str: str, sz_str: str, tif: str, reduce_only: bool) -> Dict[str, Any]:
    """
    Submit using the *current* SDK signature first, then progressively
    degrade to older call styles if needed. Emits warnings but never hides
    the underlying exception.
    """
    ex = _get_exchange()

    # Try the canonical (current) signature.
    try:
        order_type = _make_order_type(tif)
        resp = ex.order(
            coin,                   # name: str (coin symbol, e.g., "BTC")
            bool(is_buy),           # is_buy: bool
            float(sz_str),          # sz: float
            float(px_str),          # limit_px: float
            order_type,             # order_type: OrderType
            bool(reduce_only),      # reduce_only: bool
            None,                   # cloid
            None,                   # builder
        )
        return {"ok": True, "style": "positional+OrderType", "resp": resp}
    except Exception as e:
        log.warning(f"[WARN] positional+OrderType failed: {e}")

    # Try positional without OrderType (some older wrappers)
    try:
        resp = ex.order(coin, bool(is_buy), float(sz_str), float(px_str), {"limit": {"tif": tif or "Gtc"}}, bool(reduce_only))  # type: ignore[arg-type]
        return {"ok": True, "style": "positional+dict-ordertype", "resp": resp}
    except Exception as e:
        log.warning(f"[WARN] positional+dict-ordertype failed: {e}")

    # Try bulk_orders with dict payload
    try:
        payload = {
            "coin": coin,
            "is_buy": bool(is_buy),
            "sz": float(sz_str),
            "limit_px": float(px_str),
            "order_type": {"limit": {"tif": tif or "Gtc"}},
            "reduce_only": bool(reduce_only),
        }
        resp = ex.bulk_orders([payload])
        return {"ok": True, "style": "bulk_orders", "resp": resp}
    except Exception as e:
        log.warning(f"[WARN] bulk_orders failed: {e}")

    raise RuntimeError("All SDK order call styles failed.")

# ---------- Public API ----------
def submit_signal(sig) -> None:
    """
    Entry point called by execution.execute_signal.
    """
    log.info("[BROKER] hyperliquid.py loaded, version=hl-broker-1.3.0")

    symbol = sig.symbol
    if ONLY_EXECUTE:
        allowed = ",".join(ONLY_EXECUTE)
        log.info(f"[BROKER] symbol={symbol} allowed={allowed}")
        if symbol not in ONLY_EXECUTE:
            log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}")
            return

    # Required fields check (parser already provides these)
    if sig.entry_low is None or sig.entry_high is None:
        raise ValueError("Signal missing entry band")
    if sig.stop_loss is None:
        raise ValueError("Signal missing stop loss")

    plan = _build_order_plan(
        side=sig.side,
        symbol=sig.symbol,
        band_low=sig.entry_low,
        band_high=sig.entry_high,
        stop_loss=sig.stop_loss,
        leverage=sig.leverage or 20.0,
        tif=TIF_ENV,
    )

    resp = _place_order_real(
        is_buy=plan.is_buy,
        coin=plan.coin,
        px_str=plan.px_str,
        sz_str=plan.sz_str,
        tif=plan.tif,
        reduce_only=plan.reduce_only,
    )
    log.info(f"[BROKER] order submit ok style={resp.get('style')} resp={resp.get('resp')}")
