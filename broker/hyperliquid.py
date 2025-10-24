import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple, Any, List

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

# =========================
# Dynamic SDK compatibility
# =========================
def _resolve_hl() -> Tuple[Any, Any, Any, str]:
    """
    Returns (Exchange, Info, Wallet, layout_tag)
    Tries multiple import layouts so we work with whatever wheel is actually installed.
    """
    try:
        import hyperliquid as hl
        version = getattr(hl, "__version__", "unknown")
        log.info("[BROKER] hyperliquid base module=%s version=%s", getattr(hl, "__file__", "?"), version)
    except Exception:
        version = "unknown"

    # Layout A: 0.4.x submodules (preferred)
    try:
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.info import Info          # type: ignore
        from hyperliquid.wallet import Wallet      # type: ignore
        log.info("[BROKER] Using layout A (submodules: exchange/info/wallet)")
        return Exchange, Info, Wallet, "A"
    except Exception:
        pass

    # Layout B: top-level (some builds expose Exchange/Info top-level, Wallet in wallet)
    try:
        from hyperliquid import Exchange, Info     # type: ignore
        from hyperliquid.wallet import Wallet      # type: ignore
        log.info("[BROKER] Using layout B (top-level Exchange/Info, wallet submodule)")
        return Exchange, Info, Wallet, "B"
    except Exception:
        pass

    # Layout C: everything top-level (rare)
    try:
        from hyperliquid import Exchange, Info, Wallet  # type: ignore
        log.info("[BROKER] Using layout C (all top-level)")
        return Exchange, Info, Wallet, "C"
    except Exception as e:
        raise RuntimeError(
            "Could not resolve Hyperliquid SDK layout. Ensure only one HL package is installed."
        ) from e


# =========================
# Datatypes
# =========================
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


# =========================
# Config
# =========================
DEFAULT_ALLOWED = "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,MNT/USD,PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD"
ALLOWED = set(os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", DEFAULT_ALLOWED).split(","))

DEFAULT_SZ_DP = int(os.getenv("HYPER_SIZE_DP", "4"))     # e.g. 0.0001 BTC
DEFAULT_PX_DP = int(os.getenv("HYPER_PRICE_DP", "2"))    # e.g. $xxxxx.xx


# =========================
# Utilities
# =========================
def _mk_clients() -> Tuple[Any, Any, str]:
    """Construct Wallet->Exchange and Info using whichever SDK shape is present."""
    Exchange, Info, Wallet, layout = _resolve_hl()

    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError("HYPER_PRIVATE_KEY is required (0x-prefixed EVM private key).")

    # Build Wallet then Exchange — this works on all recent HL wheels
    try:
        wallet = Wallet.from_key(priv)  # hex key (with/without 0x)
    except Exception as e:
        raise RuntimeError(f"Wallet.from_key failed: {e}")

    # Some older builds require keyword 'wallet=', some accept positional; we normalize to kw.
    try:
        ex = Exchange(wallet=wallet)
    except TypeError:
        # Very old builds might want Exchange(wallet) positional
        ex = Exchange(wallet)

    info = Info()
    log.info("[BROKER] hyperliquid.py loaded (layout=%s)", layout)
    return ex, info, layout


def _symbol_to_coin(symbol: str) -> str:
    return symbol.split("/", 1)[0] if "/" in symbol else symbol


def _clamp_precision(x: float, dp: int) -> float:
    factor = 10 ** dp
    return int(round(x * factor)) / factor


def _plan_from_signal(sig: ExecSignal, info: Any) -> Plan:
    if sig.symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
        raise RuntimeError("Symbol not allowed")

    if sig.entry_low is None or sig.entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    coin = _symbol_to_coin(sig.symbol)
    is_buy = sig.side.upper() == "LONG"
    mid_px = (float(sig.entry_low) + float(sig.entry_high)) / 2.0

    limit_px = _clamp_precision(float(mid_px), DEFAULT_PX_DP)

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
        tif_post_only=True,
        reduce_only=False,
    )


def _order_type_candidates() -> List[dict]:
    """
    Different HL SDKs accept different wire shapes. We’ll try the common, valid ones:
      - {"limit": {"tif": "Alo"}}  # Add Liquidity Only (post-only)
      - {"postOnly": {}}           # some older builds
      - {"limit": {"tif": "Ioc"}}  # IOC fallback (not used by default plan)
    """
    return [
        {"limit": {"tif": "Alo"}},
        {"postOnly": {}},
        {"limit": {"tif": "Ioc"}},
    ]


def _build_order(plan: Plan, order_type: dict) -> dict:
    return {
        "coin": plan.coin,
        "is_buy": bool(plan.is_buy),
        "sz": float(plan.sz),
        "limit_px": float(plan.limit_px),
        "order_type": order_type,
        "reduce_only": bool(plan.reduce_only),
    }


def _try_bulk_with_rounding(ex: Any, order: dict) -> dict:
    """
    Retry around:
      - float_to_wire rounding
      - order_type shape differences
    We iterate order_type candidates and nudge 'sz' down by one size tick across a few attempts.
    """
    last_err: Optional[Exception] = None
    size_step = 10 ** (-DEFAULT_SZ_DP)

    for ot in _order_type_candidates():
        # reset to candidate order_type
        order["order_type"] = ot
        for _ in range(3):
            try:
                return ex.bulk_orders([order])
            except Exception as e:
                last_err = e
                # Nudge size down one precision step and retry
                order["sz"] = max(0.0, float(order["sz"]) - size_step)

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")


# =========================
# Entry point
# =========================
def submit_signal(sig: ExecSignal) -> None:
    ex, info, layout = _mk_clients()
    plan = _plan_from_signal(sig, info)

    log.info(
        "[BROKER] %s %s band=(%.6f,%.6f) SL=%s lev=%s TIF=%s",
        "BUY" if plan.is_buy else "SELL",
        sig.symbol,
        float(sig.entry_low),
        float(sig.entry_high),
        str(sig.stop_loss),
        str(sig.leverage),
        "PostOnly" if plan.tif_post_only else "IOC",
    )
    log.info(
        "[BROKER] PLAN side=%s coin=%s px=%.8f sz=%.*f reduceOnly=%s",
        "BUY" if plan.is_buy else "SELL",
        plan.coin,
        plan.limit_px,
        DEFAULT_SZ_DP,
        plan.sz,
        plan.reduce_only,
    )

    # Try the order with compatibility fallbacks
    order = _build_order(plan, {"limit": {"tif": "Alo"}})
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders resp: %s", resp)
