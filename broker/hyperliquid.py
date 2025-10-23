import os
import logging
from typing import Dict, Any, Optional, Tuple

from hyperliquid import Exchange, Info  # SDK

log = logging.getLogger("broker.hyperliquid")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ---------- Environment ----------
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "50"))
DEFAULT_TIF   = os.getenv("HYPER_TIF", "PostOnly")  # "PostOnly" or "Gtc" are typical
ALLOWED = [s.strip().upper() for s in os.getenv(
    "HYPER_ONLY_EXECUTE_SYMBOLS",
    "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,MNT/USD,PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD"
).split(",") if s.strip()]

# ---------- Helpers ----------
def _symbol_to_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    if "/" in symbol:
        return symbol.split("/")[0].strip().upper()
    return symbol.strip().upper()

def _choose_px(entry_low: float, entry_high: float, side: str) -> float:
    # Use inner edge of the band consistent with side
    if side.upper() == "LONG":
        return float(entry_low)
    return float(entry_high)

def _quantize_attempts() -> Tuple[list[int], list[int]]:
    # Decimals for (price_decimals, size_decimals) to try in order
    # HL accepts up to 8, but some coins require coarser steps
    px_try = [8, 7, 6, 5, 4, 3, 2]
    sz_try = [8, 7, 6, 5, 4, 3, 2]
    return px_try, sz_try

def _mk_clients() -> Tuple[Exchange, Info]:
    # Wallet private key auth (recommended)
    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")
    # Current SDK accepts 'Exchange(private_key)' positional
    try:
        ex = Exchange(priv)  # wallet key
        log.info("[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY=%s…%s",
                 priv[:6], priv[-4:])
    except TypeError as e:
        # Fallback: raise explicit guidance
        raise RuntimeError("Your installed hyperliquid SDK does not accept private key positional init.") from e

    info = Info()
    return ex, info

def _build_order_dict(coin: str, is_buy: bool, sz: float, limit_px: float, tif: str) -> Dict[str, Any]:
    # SDK order_request format expected by exchange.bulk_orders([...])
    return {
        "coin": coin,                # name (not asset id)
        "is_buy": bool(is_buy),
        "sz": float(sz),             # must match coin step
        "limit_px": float(limit_px), # must match price tick
        "order_type": {"limit": {"tif": tif}},  # e.g., "PostOnly" or "Gtc"
        "reduce_only": False,
        "cloid": None,
    }

def _try_bulk_with_rounding(ex: Exchange, order: Dict[str, Any]) -> Any:
    """
    Retry bulk_orders by progressively reducing precision on sz/limit_px
    until SDK accepts (avoids `float_to_wire causes rounding`).
    """
    px_attempts, sz_attempts = _quantize_attempts()
    last_err: Optional[Exception] = None

    for pd in px_attempts:
        for sd in sz_attempts:
            try:
                # Quantize and try
                order_try = dict(order)
                order_try["limit_px"] = float(round(order["limit_px"], pd))
                order_try["sz"] = float(round(order["sz"], sd))
                # Ensure strictly positive size
                if order_try["sz"] <= 0:
                    continue
                return ex.bulk_orders([order_try])
            except Exception as e:
                last_err = e

    # One last coarse attempt at 1–3 decimals for price & 3–5 for size
    for pd in (3, 2, 1):
        for sd in (5, 4, 3):
            try:
                order_try = dict(order)
                order_try["limit_px"] = float(round(order["limit_px"], pd))
                order_try["sz"] = float(round(order["sz"], sd))
                if order_try["sz"] <= 0:
                    continue
                return ex.bulk_orders([order_try])
            except Exception as e:
                last_err = e

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# ---------- Public entry ----------
def submit_signal(sig) -> None:
    """
    Expected fields on `sig` (ExecSignal):
      - side: "LONG" or "SHORT"
      - symbol: like "BTC/USD"
      - entry_low, entry_high: floats
      - stop_loss: Optional[float]
      - leverage: Optional[float]
      - tpn: Optional[int]
      - timeframe: Optional[str]
      - tif: Optional[str]
    """
    side = str(getattr(sig, "side")).upper()
    symbol = str(getattr(sig, "symbol")).upper()

    tif = getattr(sig, "tif", None) or DEFAULT_TIF
    if tif not in ("PostOnly", "Gtc"):
        tif = DEFAULT_TIF

    if ALLOWED and symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    entry_low = float(getattr(sig, "entry_low"))
    entry_high = float(getattr(sig, "entry_high"))
    is_buy = True if side == "LONG" else False
    coin = _symbol_to_coin(symbol)
    px = _choose_px(entry_low, entry_high, side)

    ex, info = _mk_clients()

    # Compute size ~ USD_PER_TRADE / price
    try:
        sz = USD_PER_TRADE / float(px)
    except Exception:
        raise RuntimeError("Invalid entry price computed for order size.")

    order = _build_order_dict(coin=coin, is_buy=is_buy, sz=sz, limit_px=px, tif=tif)

    log.info(
        "[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=%s",
        side, symbol, entry_low, entry_high,
        str(getattr(sig, "stop_loss", None)),
        str(getattr(sig, "leverage", None)),
        tif,
    )

    # Plan log (mid-band px)
    mid_px = (entry_low + entry_high) / 2.0
    sz_preview = USD_PER_TRADE / max(px, 1e-9)
    log.info("[PLAN] side=%s coin=%s px=%0.8f sz=%s tif=%s reduceOnly=False",
             "BUY" if is_buy else "SELL", coin, mid_px, f"{sz_preview:.8f}", tif)

    try:
        resp = _try_bulk_with_rounding(ex, order)
        log.info("[BROKER] bulk_orders response: %s", str(resp))
    except Exception as e:
        # Surface the most useful error
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e
