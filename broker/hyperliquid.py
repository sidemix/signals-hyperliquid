# broker/hyperliquid.py
import os
import logging
from typing import Optional, Tuple

from .types import Side
# ^ If you don't have a local types module, delete this import and use simple booleans via (side.upper()=="LONG")

log = logging.getLogger("broker.hyperliquid")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"), format="%(levelname)s:%(name)s:%(message)s")

# --- Hyperliquid SDK imports & compatibility shims ---------------------------
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.signing import OrderType, TIF  # modern SDK
    _ORDER_LIMIT_CLASS = True
except Exception:
    # Older/lighter SDKs — fall back to string payloads
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    OrderType = None  # type: ignore
    TIF = None        # type: ignore
    _ORDER_LIMIT_CLASS = False

VERSION = "hl-broker-compat-2.3"
log.info("[BROKER] hyperliquid.py loaded, version=%s", VERSION)

# --- Env ---------------------------------------------------------------------
ALLOW = [s.strip() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()]
DEFAULT_TIF = os.getenv("HYPER_TIF", "PostOnly").strip().lower()  # postonly|gtc|ioc
NETWORK = os.getenv("HYPER_NETWORK", "mainnet").strip().lower()
BASE_URL = os.getenv("HYPERLIQUID_BASE", "").strip() or None
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY", "").strip()

NOTIONAL_USD = float(os.getenv("HYPER_NOTIONAL_USD", "0") or 0)
FIXED_QTY = float(os.getenv("HYPER_FIXED_QTY", "0") or 0)

# --- Helpers -----------------------------------------------------------------
def _split_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    return symbol.split("/")[0].strip().upper()

def _mk_clients() -> Tuple[Exchange, Info]:
    """
    Build Exchange + Info clients using wallet private key.
    Try a few constructor signatures for SDK compatibility.
    """
    if not PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    # Prefer mainnet default endpoints; allow override via HYPERLIQUID_BASE
    base_kw = {}
    if BASE_URL:
        # Newer SDK uses base_url; some older use exchange-url via env
        base_kw = {"base_url": BASE_URL}

    # Try different constructors the SDK has used across versions
    last_err = None
    for ctor in (
        lambda: Exchange(PRIVKEY, **base_kw),                 # most common
        lambda: Exchange(private_key=PRIVKEY, **base_kw),     # some builds
    ):
        try:
            ex = ctor()
            info = Info(**base_kw) if base_kw else Info()
            log.info("[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY=%s…%s",
                     PRIVKEY[:6], PRIVKEY[-4:])
            return ex, info
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Could not initialize Exchange with provided private key: {last_err}")

def _order_type_from_env() -> object:
    tif_key = DEFAULT_TIF
    if _ORDER_LIMIT_CLASS:
        # Use the modern class API
        if tif_key in ("postonly", "po", "post_only"):
            return OrderType.Limit(tif=TIF.Po)
        if tif_key in ("ioc",):
            return OrderType.Limit(tif=TIF.Ioc)
        return OrderType.Limit(tif=TIF.Gtc)
    else:
        # Fallback JSON wire format
        if tif_key in ("postonly", "po", "post_only"):
            return {"t": "limit", "tif": "Po"}
        if tif_key in ("ioc",):
            return {"t": "limit", "tif": "Ioc"}
        return {"t": "limit", "tif": "Gtc"}

def _size_from_notional(notional_usd: float, px: float) -> float:
    if px <= 0:
        return 0.0
    return max(0.0, round(notional_usd / px, 8))

# --- Public entry -------------------------------------------------------------
def submit_signal(sig) -> None:
    """
    Submit a single LIMIT order at the mid of the entry band using HL SDK.
    """
    # Allowed symbols gate
    allowed = ",".join(ALLOW) if ALLOW else "(all)"
    log.info("[BROKER] symbol=%s allowed=%s", sig.symbol, allowed)
    if ALLOW and sig.symbol not in ALLOW:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
        return

    # Extract values
    coin_name = _split_coin(sig.symbol)          # "BTC"
    is_buy = sig.side.strip().upper() == "LONG"
    band_low = float(sig.entry_low)
    band_high = float(sig.entry_high)

    if not (band_low and band_high):
        raise ValueError("Signal missing entry_band=(low, high).")

    # Mid entry price (float)
    px_entry = round((band_low + band_high) / 2.0, 8)

    # Determine size: fixed qty (if provided) else notional / price
    if FIXED_QTY > 0:
        sz_val = round(FIXED_QTY, 8)
    elif NOTIONAL_USD > 0:
        sz_val = _size_from_notional(NOTIONAL_USD, px_entry)
    else:
        # Default to tiny sizing so tests can still go through
        sz_val = 0.0001

    order_type = _order_type_from_env()

    log.info(
        "[BROKER] %s %s band=(%.6f,%.6f) SL=%.6f lev=%s TIF=%s",
        "LONG" if is_buy else "SHORT",
        sig.symbol,
        band_low, band_high,
        float(sig.stop_loss),
        str(getattr(sig, "leverage", "")),
        DEFAULT_TIF,
    )

    ex, info = _mk_clients()

    # Build order payload with floats (NOT strings)
    order = {
        "coin": coin_name,
        "is_buy": bool(is_buy),
        "sz": float(sz_val),
        "limit_px": float(px_entry),
        "order_type": order_type,
        "reduce_only": False,
    }

    # Submit with bulk_orders (SDK canonical path)
    try:
        resp = ex.bulk_orders([order])
        log.info("[BROKER] bulk_orders response: %s", str(resp)[:300])
    except Exception as e:
        # Surface clean error; this is where the old 'Unknown format code f' happened.
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e
