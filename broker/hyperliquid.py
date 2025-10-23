"""
broker/hyperliquid.py
Lightweight HL broker adapter (SDK >= 0.20.x).

Fixes:
- Remove import/use of TIF enum (build OrderType.Limit(tif="Gtc") directly)
- Read entry band via ExecSignal.entry_band instead of band_low/band_high
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import OrderType  # NOTE: no TIF import

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

BROKER_VERSION = "hl-broker-1.2.3"


# --------------------------- env helpers ---------------------------

def _get_env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    return v if (v is not None and str(v).strip() != "") else default


def _get_env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    s = v.strip().lower()
    return s in ("1", "true", "yes", "on")


def _csv_set(name: str) -> set[str]:
    raw = _get_env_str(name, "")
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


# --------------------------- config ---------------------------

HYPER_BASE = _get_env_str("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz")
HYPER_NETWORK = _get_env_str("HYPER_NETWORK", "mainnet")
HYPER_TIF = _get_env_str("HYPER_TIF", "Gtc") or "Gtc"   # "Gtc" | "Ioc" (string)
DEFAULT_LEV = float(_get_env_str("HYPER_DEFAULT_LEVERAGE", "20") or "20")

NOTIONAL_USD = _get_env_str("HYPER_NOTIONAL_USD")  # preferred
FIXED_QTY = _get_env_str("HYPER_FIXED_QTY")        # fallback

ONLY_EXEC = _csv_set("HYPER_ONLY_EXECUTE_SYMBOLS")
DRY_RUN = _get_env_bool("DRY_RUN", False) or _get_env_bool("HYPER_DRY_RUN", False)

# keys: either use normal wallet privkey or API/agent key (both are standard secp256k1)
PRIVKEY = _get_env_str("HYPER_AGENT_PRIVATE_KEY") or _get_env_str("HYPER_EVM_PRIVKEY")
if not PRIVKEY:
    log.warning("[BROKER] No private key found in HYPER_AGENT_PRIVATE_KEY or HYPER_EVM_PRIVKEY; live orders will fail.")

# clients
_info: Optional[Info] = None
_ex: Optional[Exchange] = None


def _ensure_clients() -> tuple[Info, Exchange]:
    global _info, _ex
    if _info is None:
        _info = Info(base_url=HYPER_BASE, skip_ws=True)
    if _ex is None:
        _ex = Exchange(PRIVKEY, base_url=HYPER_BASE)
    return _info, _ex


# --------------------------- datatypes ---------------------------

@dataclass
class _Plan:
    coin: str
    is_buy: bool
    sz: float
    limit_px: float
    tif: str
    reduce_only: bool = False


# --------------------------- utilities ---------------------------

def _symbol_to_coin(symbol: str) -> str:
    """
    "ETH/USD" -> "ETH"
    """
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/", 1)[0]
    return s


def _mid_of_band(entry_band: Tuple[float, float]) -> float:
    lo, hi = entry_band
    return (float(lo) + float(hi)) / 2.0


def _decide_size_usd(coin: str, mid_px: float) -> float:
    """
    Returns contract size in coin. Prefers notional sizing; falls back to fixed qty.
    """
    if NOTIONAL_USD:
        notional = float(NOTIONAL_USD)
        if mid_px <= 0:
            raise RuntimeError("Invalid mid price for sizing")
        return notional / mid_px
    if FIXED_QTY:
        return float(FIXED_QTY)
    # ultra-conservative fallback
    return 1.0


def _build_plan(side: str,
                symbol: str,
                entry_band: Tuple[float, float],
                tif: str) -> _Plan:
    coin = _symbol_to_coin(symbol)
    is_buy = side.strip().upper() == "LONG"
    px = _mid_of_band(entry_band)
    sz = _decide_size_usd(coin, px)
    return _Plan(coin=coin, is_buy=is_buy, sz=sz, limit_px=px, tif=tif)


def _order_type_from_tif(tif: str) -> OrderType:
    # Build a proper OrderType dynamically (no enum import needed)
    tif_norm = (tif or "Gtc").strip().capitalize()
    if tif_norm not in ("Gtc", "Ioc"):
        tif_norm = "Gtc"
    return OrderType.Limit(tif=tif_norm)


# --------------------------- public entry ---------------------------

def submit_signal(sig) -> None:
    """
    Adapter called by execution.py. Expects a dataclass-like object with at least:
      - side: "LONG" | "SHORT"
      - symbol: "ETH/USD", etc.
      - entry_band: tuple(low, high)
      - stop_loss: float (best-effort; not set here)
      - leverage: float (best-effort; not set here)
    """
    log.info("[BROKER] hyperliquid.py loaded, version=%s", BROKER_VERSION)

    # Validate symbol allowlist, if provided
    if ONLY_EXEC:
        if sig.symbol.upper() not in (s.upper() for s in ONLY_EXEC):
            log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
            return

    # Pull band from ExecSignal (use entry_band, do not expect band_low/band_high)
    entry_band = getattr(sig, "entry_band", None)
    if not entry_band or not isinstance(entry_band, (tuple, list)) or len(entry_band) != 2:
        raise ValueError("Signal missing entry_band=(low, high).")

    side = getattr(sig, "side")
    symbol = getattr(sig, "symbol")
    tif = _get_env_str("HYPER_TIF", HYPER_TIF) or "Gtc"

    plan = _build_plan(side, symbol, (float(entry_band[0]), float(entry_band[1])), tif)

    log.info(
        "[PLAN] side=%s coin=%s px=%.8f sz=%.8f tif=%s reduceOnly=%s",
        "BUY" if plan.is_buy else "SELL", plan.coin, plan.limit_px, plan.sz, plan.tif, plan.reduce_only
    )

    if DRY_RUN:
        log.info("[DRYRUN] submit LIMIT %s %s px=%.8f sz=%.8f tif=%s",
                 "BUY" if plan.is_buy else "SELL", plan.coin, plan.limit_px, plan.sz, plan.tif)
        return

    if not PRIVKEY:
        raise RuntimeError("No private key configured; cannot place live orders. Set HYPER_AGENT_PRIVATE_KEY or HYPER_EVM_PRIVKEY.")

    # clients
    info, ex = _ensure_clients()

    # Build the SDK payload and place the order using the *positional* signature:
    #   Exchange.order(name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None, builder=None)
    order_type = _order_type_from_tif(plan.tif)

    try:
        resp = ex.order(plan.coin, plan.is_buy, float(plan.sz), float(plan.limit_px), order_type, plan.reduce_only)
        log.info("[LIVE] order response: %s", resp)
    except Exception as e:
        log.exception("[ERR] SDK order failed: %s", e)
        raise
