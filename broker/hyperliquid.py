import os
import math
import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

VERSION = "hl-broker-1.2.2"

# ---- env helpers -------------------------------------------------------------

def _env_bool(k: str, default: bool) -> bool:
    v = os.getenv(k)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}

def _env_float(k: str, default: Optional[float]) -> Optional[float]:
    v = os.getenv(k)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except Exception:
        log.warning("Invalid float for %s=%r; using %r", k, v, default)
        return default

# public allow-list, same key your logs showed earlier
_ALLOWED = (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS") or
            "ETH/USD,BTC/USD,SOL/USD,LINK/USD,BNB/USD,AVAX/USD").split(",")

DRYRUN = _env_bool("HYPER_DRY_RUN", False)
TIF = os.getenv("HYPER_TIF", "Gtc")  # Gtc|Ioc|Fok (sdk handles internally)
NOTIONAL_USD = _env_float("HYPER_NOTIONAL_USD", None)
FIXED_QTY = _env_float("HYPER_FIXED_QTY", None)

# Credentials: **use your wallet’s PRIVATE KEY** (hex w/o 0x or with 0x).
# Do NOT put API key here; HL signs orders with your wallet key.
OWNER = os.getenv("HYPER_OWNER")            # your wallet address (0x...)
PRIVATE_KEY = os.getenv("HYPER_TRADER_KEY") # your wallet private key
BASE_URL = os.getenv("HYPER_BASE_URL", "https://api.hyperliquid.xyz")
WS_URL = os.getenv("HYPER_WS_URL", "wss://api.hyperliquid.xyz/ws")

# ---- safe sdk import (runtime) ----------------------------------------------

def _sdk():
    """
    Import HL SDK lazily so import-time errors in this file never kill the bot.
    """
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.signing import OrderType
    return Exchange, Info, OrderType

# ---- utility ----------------------------------------------------------------

def _symbol_to_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    s = symbol.strip().upper()
    return s.split("/")[0] if "/" in s else s

def _size_from_env(price: float) -> float:
    if FIXED_QTY and FIXED_QTY > 0:
        return FIXED_QTY
    if NOTIONAL_USD and NOTIONAL_USD > 0 and price > 0:
        return NOTIONAL_USD / price
    # sensible tiny default (prevents 0-size)
    return max(0.001, 50.0 / max(price, 1.0))

def _mid(a: float, b: float) -> float:
    return (float(a) + float(b)) / 2.0

# ---- submission --------------------------------------------------------------

def _place_order_real(*, name: str, is_buy: bool, sz: float, limit_px: float, tif: str):
    """
    Single, canonical SDK call path that matches the signature you printed:
    Exchange.order(name: str, is_buy: bool, sz: float, limit_px: float,
                   order_type: hyperliquid.utils.signing.OrderType,
                   reduce_only: bool=False, cloid: Optional[...] = None,
                   builder: Optional[...] = None)
    """
    Exchange, Info, OrderType = _sdk()

    if not OWNER or not PRIVATE_KEY:
        raise RuntimeError("Missing HYPER_OWNER / HYPER_TRADER_KEY env vars.")

    ex = Exchange(base_url=BASE_URL, account_address=OWNER, key=PRIVATE_KEY)
    info = Info(base_url=BASE_URL, websocket_url=WS_URL)

    # Build order type. SDK defaults GTC for Limit() if tif not provided;
    # where available, we pass tif explicitly.
    # Some SDK versions model tif inside OrderType; others infer from order flags.
    try:
        # Newer SDKs: OrderType.Limit(tif="Gtc"|"Ioc"|"Fok")
        ot = OrderType.Limit(tif)
    except TypeError:
        # Older SDKs: OrderType.Limit() with implicit GTC (still fine)
        ot = OrderType.Limit()

    # Execute
    resp = ex.order(name, bool(is_buy), float(sz), float(limit_px), ot, reduce_only=False)
    return resp

# ---- external API used by execution.py --------------------------------------

def submit_signal(sig) -> None:
    """
    Called from execution.py
      expected attributes on sig:
        side, symbol, band_low, band_high, stop_loss, leverage, timeframe
    """
    log.info("[BROKER] hyperliquid.py loaded, version=%s", VERSION)

    symbol = getattr(sig, "symbol")
    side = getattr(sig, "side")
    band_low = float(getattr(sig, "band_low"))
    band_high = float(getattr(sig, "band_high"))

    if symbol not in [s.strip() for s in _ALLOWED if s.strip()]:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    coin = _symbol_to_coin(symbol)
    px = _mid(band_low, band_high)
    is_buy = side.upper() == "LONG"
    sz = _size_from_env(px)

    log.info(
        "[PLAN] side=%s coin=%s px=%.8f sz=%s tif=%s reduceOnly=%s",
        "BUY" if is_buy else "SELL",
        coin,
        px,
        sz,
        TIF,
        False,
    )

    if DRYRUN:
        log.info("[DRYRUN] submit LIMIT %s %s px=%.8f sz=%s tif=%s",
                 "BUY" if is_buy else "SELL", coin, px, sz, TIF)
        return

    # live path
    try:
        resp = _place_order_real(name=coin, is_buy=is_buy, sz=sz, limit_px=px, tif=TIF)
        log.info("[BROKER] order submit ok resp=%r", resp)
    except Exception as e:
        log.exception("Order placement failed: %s", e)
        raise
