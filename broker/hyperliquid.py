import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple

# --- Hyperliquid 0.4.66 imports (submodules) ---
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.wallet import Wallet

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

# Symbols you actually want to allow the bot to trade
DEFAULT_ALLOWED = "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,MNT/USD,PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD"
ALLOWED = set(os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", DEFAULT_ALLOWED).split(","))

# Size / price precision guards to avoid "float_to_wire causes rounding"
# BTC typically supports 1e-4 size precision and ~0.5 price tick.
# We keep this conservative to satisfy wire rounding:
DEFAULT_SZ_DP = int(os.getenv("HYPER_SIZE_DP", "4"))     # e.g., 0.0001 BTC
DEFAULT_PX_DP = int(os.getenv("HYPER_PRICE_DP", "2"))    # e.g., $109525.00

# PostOnly maker tif in wire shape for 0.4.66
POST_ONLY_ORDER_TYPE = {"limit": {"tif": "Alo"}}  # ALO = Add Liquidity Only (post-only)
IOC_ORDER_TYPE       = {"limit": {"tif": "Ioc"}}

@dataclass
class ExecSignal:
    side: str                 # "LONG" | "SHORT"
    symbol: str               # "BTC/USD"
    entry_low: float
    entry_high: float
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tf: Optional[str] = None  # not used by broker

@dataclass
class Plan:
    is_buy: bool
    coin: str
    limit_px: float
    sz: float
    tif_post_only: bool
    reduce_only: bool = False


def _mk_clients() -> Tuple[Exchange, Info]:
    """Construct Wallet->Exchange and Info for SDK 0.4.66."""
    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError("HYPER_PRIVATE_KEY is required (0x-prefixed EVM private key).")

    # Wallet.from_key accepts hex string (with or without 0x)
    wallet = Wallet.from_key(priv)
    ex = Exchange(wallet=wallet)
    info = Info()
    log.info("[BROKER] hyperliquid.py loaded")
    return ex, info


def _symbol_to_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    if "/" in symbol:
        return symbol.split("/", 1)[0]
    return symbol


def _clamp_precision(x: float, dp: int) -> float:
    # Round to dp decimals in a wire-friendly way
    factor = 10 ** dp
    return int(round(x * factor)) / factor


def _plan_from_signal(sig: ExecSignal, info: Info) -> Plan:
    if sig.symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
        raise RuntimeError("Symbol not allowed")

    if sig.entry_low is None or sig.entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    coin = _symbol_to_coin(sig.symbol)
    is_buy = sig.side.upper() == "LONG"
    mid_px = (float(sig.entry_low) + float(sig.entry_high)) / 2.0

    # Clamp price and size
    limit_px = _clamp_precision(float(mid_px), DEFAULT_PX_DP)

    # Position sizing: you probably have your own calc; we just do a tiny notional
    # e.g., $50 notional divided by price -> BTC size
    target_notional = float(os.getenv("HYPER_TEST_NOTIONAL_USD", "50"))
    raw_sz = target_notional / max(limit_px, 1e-9)
    sz = _clamp_precision(raw_sz, DEFAULT_SZ_DP)

    if sz <= 0:
        raise ValueError("Computed size is zero; increase HYPER_TEST_NOTIONAL_USD.")

    return Plan(
        is_buy=is_buy,
        coin=coin,
        limit_px=limit_px,
        sz=sz,
        tif_post_only=True,       # default to post-only maker
        reduce_only=False,
    )


def _build_order(plan: Plan) -> dict:
    order_type = POST_ONLY_ORDER_TYPE if plan.tif_post_only else IOC_ORDER_TYPE
    order = {
        "coin": plan.coin,
        "is_buy": bool(plan.is_buy),
        "sz": float(plan.sz),
        "limit_px": float(plan.limit_px),
        "order_type": order_type,
        "reduce_only": bool(plan.reduce_only),
    }
    return order


def _try_bulk_with_rounding(ex: Exchange, order: dict) -> dict:
    """Retry around wire rounding by nudging size to the allowed dp."""
    last_err: Optional[Exception] = None

    for _ in range(3):
        try:
            return ex.bulk_orders([order])  # returns SDK response dict
        except Exception as e:
            last_err = e
            # Nudge size down one unit of precision and try again
            step = 10 ** (-DEFAULT_SZ_DP)
            order["sz"] = max(0.0, float(order["sz"]) - step)

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")


def submit_signal(sig: ExecSignal) -> None:
    """Main entrypoint called by your execution layer."""
    ex, info = _mk_clients()

    plan = _plan_from_signal(sig, info)
    log.info(
        "[BROKER] %s %s band=(%.6f,%.6f) SL=%s lev=%s TIF=%s",
        "BUY" if plan.is_buy else "SELL",
        f"{sig.symbol}",
        float(sig.entry_low),
        float(sig.entry_high),
        str(sig.stop_loss),
        str(sig.leverage),
        "PostOnly" if plan.tif_post_only else "IOC",
    )

    log.info(
        "[BROKER] PLAN side=%s coin=%s px=%.8f sz=%.*f tif=%s reduceOnly=%s",
        "BUY" if plan.is_buy else "SELL",
        plan.coin,
        plan.limit_px,
        DEFAULT_SZ_DP,
        plan.sz,
        "PostOnly" if plan.tif_post_only else "IOC",
        plan.reduce_only,
    )

    order = _build_order(plan)
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders resp: %s", resp)
