import os
import math
import logging as log
from typing import Any, Dict, Tuple, Optional

# ---------- Configuration ----------
# Symbols you actually want to auto-execute (comma-separated). Leave blank to allow all.
_ALLOWED = os.getenv(
    "HYPER_ONLY_EXECUTE_SYMBOLS",
    "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,"
    "MNT/USD,PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD"
)
ALLOWED = {s.strip().upper() for s in _ALLOWED.split(",") if s.strip()}

# USD notional per order (before leverage). You can override with HYPER_ORDER_USD.
DEFAULT_NOTIONAL_USD = float(os.getenv("HYPER_ORDER_USD", "50"))

# Choose default TIF semantics you prefer; we’ll try these variants in order:
# - "Alo" is “add liquidity only” on older SDKs (same idea as PostOnly)
# - "PostOnly" exists on some builds
# - "Gtc" as last resort (not strictly post-only)
_TIF_VARIANTS = ["Alo", "PostOnly", "Gtc"]

# Max rounding attempts when SDK complains about float_to_wire or precision
_MAX_ROUND_TRIES = 6  # gradually reduce decimals

# ---------- Dynamic HL resolver ----------

def _resolve_hl() -> Tuple[Any, Any, Any, str]:
    """
    Dynamically locate Exchange, Info, Wallet inside the installed `hyperliquid` package,
    regardless of layout/version. Returns (Exchange, Info, Wallet, layout_tag).
    """
    import importlib
    import inspect
    import pkgutil

    try:
        import hyperliquid as hl
    except Exception as e:
        raise RuntimeError("hyperliquid package not importable") from e

    base_file = getattr(hl, "__file__", "?")
    version = getattr(hl, "__version__", "unknown")
    log.info("[BROKER] hyperliquid base module=%s version=%s", base_file, version)

    # Search candidates
    candidates = ["hyperliquid"]
    if hasattr(hl, "__path__"):
        for m in pkgutil.walk_packages(hl.__path__, prefix="hyperliquid."):
            candidates.append(m.name)

    needed = {"Exchange": None, "Info": None, "Wallet": None}
    located = {"Exchange": None, "Info": None, "Wallet": None}

    for modname in candidates:
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        for cls_name in list(needed.keys()):
            if needed[cls_name] is None:
                obj = getattr(mod, cls_name, None)
                if inspect.isclass(obj):
                    needed[cls_name] = obj
                    located[cls_name] = modname
        if all(needed.values()):
            break

    if not all(needed.values()):
        raise RuntimeError(
            "Could not resolve Hyperliquid SDK layout. "
            f"Located: {located}. Ensure only one HL package is installed."
        )

    layout_tag = f"scan:{located['Exchange']},{located['Info']},{located['Wallet']}"
    log.info("[BROKER] Using dynamic layout %s", layout_tag)
    return needed["Exchange"], needed["Info"], needed["Wallet"], layout_tag


def _mk_clients() -> Tuple[Any, Any, str]:
    """
    Build (Exchange, Info, layout_tag) using Wallet.from_key(HYPER_PRIVATE_KEY).
    Works across the SDK layouts we’ve seen in the wild.
    """
    Exchange, Info, Wallet, layout = _resolve_hl()

    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not (priv and priv.startswith("0x") and len(priv) > 10):
        raise RuntimeError("HYPER_PRIVATE_KEY is required (0x-prefixed EVM private key).")

    # Newer wheels usually expose Wallet.from_key; older may need Wallet(private_key)
    try:
        wallet = getattr(Wallet, "from_key")(priv)
    except Exception:
        try:
            wallet = Wallet(priv)  # fallback for older builds
        except Exception as e:
            raise RuntimeError(f"Could not construct Wallet: {e}")

    # Newer wheels prefer keyword
    try:
        ex = Exchange(wallet=wallet)
    except TypeError:
        # Very old builds only accept positional
        ex = Exchange(wallet)

    try:
        info = Info()
    except Exception as e:
        raise RuntimeError(f"Info() failed: {e}")

    log.info("[BROKER] hyperliquid.py loaded (layout=%s)", layout)
    return ex, info, layout

# ---------- Helpers ----------

def _coin_from_symbol(symbol: str) -> str:
    if not symbol:
        raise ValueError("Empty symbol.")
    s = symbol.upper().strip()
    if "/" in s:
        return s.split("/", 1)[0]
    # If already a coin, return as-is.
    return s

def _entry_px(sig) -> float:
    """
    Choose a limit price from the entry band. Midpoint is a good default.
    """
    low = getattr(sig, "entry_low", None)
    high = getattr(sig, "entry_high", None)
    if low is None or high is None:
        raise ValueError("Signal missing entry_band=(low, high).")
    return (float(low) + float(high)) / 2.0

def _usd_to_size(usd: float, px: float) -> float:
    """
    Convert USD notional -> coin size. Keep decent precision; we’ll round if SDK complains.
    """
    if px <= 0:
        raise ValueError("Price must be positive.")
    return usd / px

def _clamp_decimals(x: float, max_dec: int) -> float:
    """
    Round x to `max_dec` decimals safely.
    """
    fmt = "{:0." + str(max_dec) + "f}"
    return float(fmt.format(x))

def _build_order(coin: str, is_buy: bool, sz: float, limit_px: float, tif: str) -> Dict[str, Any]:
    """
    Construct an order request object that works across SDK flavors.
    Different builds expect either:
      - order_type={"limit": {"tif": "Alo"|"PostOnly"|"Gtc"}}
      - or orderType={"limit": {"tif": ...}}
    We’ll fill both keys with the same value to maximize compatibility; the SDK will
    read the one it knows and ignore the other.
    """
    order_type = {"limit": {"tif": tif}}
    return {
        "coin": coin,
        "is_buy": bool(is_buy),
        "sz": float(sz),
        "limit_px": float(limit_px),
        "order_type": order_type,   # new-ish name
        "orderType": order_type,    # older/wrapper name
        # Some SDKs accept "reduce_only" as snake_case; others as camelCase
        "reduce_only": False,
        "reduceOnly": False,
    }

def _try_bulk_with_rounding(ex: Any, order: Dict[str, Any]) -> Any:
    """
    Call ex.bulk_orders([order]) while:
      1) Trying several TIF variants (Alo, PostOnly, Gtc)
      2) Reducing size/price decimals if SDK complains about float_to_wire rounding
    """
    # extract editable fields
    base_px = float(order["limit_px"])
    base_sz = float(order["sz"])

    # Try each tif variant
    last_err: Optional[Exception] = None
    for tif in _TIF_VARIANTS:
        # plug tif into both order_type / orderType
        order["order_type"]["limit"]["tif"] = tif
        order["orderType"]["limit"]["tif"] = tif

        # progressive decimal reduction to avoid float_to_wire issues
        for dec in range(8, max(0, 8 - _MAX_ROUND_TRIES), -1):
            order["limit_px"] = _clamp_decimals(base_px, dec)
            order["sz"] = _clamp_decimals(base_sz, max(0, dec - 2))  # usually size needs fewer decimals

            try:
                return ex.bulk_orders([order])
            except Exception as e:
                msg = str(e)
                last_err = e
                # Known text patterns -> continue trying
                if any(s in msg for s in [
                    "float_to_wire", "rounding", "Unknown format code 'f'",
                    "Invalid order type", "object has no attribute 'sign_message'",
                    "byte indices must be integers", "SignableMessage",
                ]):
                    continue
                # Otherwise bubble up
                break

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# ---------- Public: submit_signal ----------

def submit_signal(sig) -> None:
    """
    Entry point called by your executor. Expects `sig` to have:
      - side: "LONG" or "SHORT"
      - symbol: "BTC/USD" etc.
      - entry_low, entry_high: floats
      - stop_loss: optional float (not used here yet)
      - leverage: optional (not used to size; size is notional in USD)
    """
    ex, info, layout = _mk_clients()

    side = str(getattr(sig, "side", "")).upper()
    symbol = str(getattr(sig, "symbol", "")).upper()
    if not side or not symbol:
        raise ValueError("Signal missing side or symbol.")

    if ALLOWED and symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    coin = _coin_from_symbol(symbol)
    is_buy = side == "LONG"

    # Pick price from entry band; compute a small USD notional -> coin size
    px = _entry_px(sig)
    notional_usd = float(os.getenv("HYPER_ORDER_USD", DEFAULT_NOTIONAL_USD))
    sz = _usd_to_size(notional_usd, px)

    # Log plan
    log.info(
        "[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=PostOnly",
        "BUY" if is_buy else "SELL",
        symbol,
        float(getattr(sig, "entry_low", 0.0)),
        float(getattr(sig, "entry_high", 0.0)),
        getattr(sig, "stop_loss", None),
        getattr(sig, "leverage", None),
    )

    order = _build_order(coin=coin, is_buy=is_buy, sz=sz, limit_px=px, tif="Alo")
    log.info(
        "[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s reduceOnly=%s",
        "BUY" if is_buy else "SELL",
        coin, order["limit_px"], order["sz"],
        order["order_type"]["limit"]["tif"],
        order.get("reduce_only", False),
    )

    # Place the order with retries for rounding/layout quirks
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders response: %s", resp)
