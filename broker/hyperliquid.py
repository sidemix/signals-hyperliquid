import os
import math
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

# --- Hyperliquid SDK imports (support multiple SDK layouts) -------------------
try:
    # Newer layout
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
except ImportError:  # very old SDKs
    from hyperliquid import Exchange, Info  # type: ignore

# Wallet wrapper name moved around in different releases
_Wallet = None
for _cand in (
    "hyperliquid.wallet",
    "hyperliquid.utils.wallet",
):
    try:
        _mod = __import__(_cand, fromlist=["Wallet"])
        _Wallet = getattr(_mod, "Wallet")
        break
    except Exception:
        pass

if _Wallet is None:
    raise RuntimeError("Could not locate hyperliquid Wallet class in this SDK build.")

# Constants location also moved across releases; we only need URLs
_BASE_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)


# --- Internal helpers ---------------------------------------------------------

def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    if v is not None and isinstance(v, str):
        v = v.strip()
    return v


def _get_allowed_symbols() -> set[str] | None:
    csv = _env("HYPER_ONLY_EXECUTE_SYMBOLS")
    if not csv:
        return None
    return {s.strip().upper() for s in csv.split(",") if s.strip()}


def _round8(x: float) -> float:
    """Round DOWN to 8 dp to avoid float_to_wire rounding error."""
    q = Decimal(str(x)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return float(q)


def _mk_clients() -> tuple[Exchange, Info, str]:
    """Create Exchange/Info using wallet private key with SDK compatibility."""
    priv = _env("HYPER_PRIVATE_KEY")
    if not priv:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    network = (_env("HYPER_NETWORK") or "mainnet").lower()
    base_url = _BASE_URLS.get(network, _BASE_URLS["mainnet"])

    # Construct wallet via SDK's Wallet wrapper
    wallet = _Wallet(priv)

    # Newer SDKs: Exchange(wallet=...), Info(base_url=...)
    # Older SDKs may accept Exchange(wallet, base_url) positionally.
    ex = None
    last_err: Exception | None = None
    for style in (
        lambda: Exchange(wallet=wallet, base_url=base_url),
        lambda: Exchange(wallet),  # positional
        lambda: Exchange(wallet=wallet),  # without base_url if SDK sets default
    ):
        try:
            ex = style()
            break
        except Exception as e:
            last_err = e

    if ex is None:
        raise RuntimeError(f"Could not construct Exchange with any style: {last_err}")

    # Info has similar constructor variability
    info = None
    last_err = None
    for style in (
        lambda: Info(base_url=base_url),
        lambda: Info(),
    ):
        try:
            info = style()
            break
        except Exception as e:
            last_err = e

    if info is None:
        raise RuntimeError(f"Could not construct Info with any style: {last_err}")

    log.info("[BROKER] hyperliquid.py loaded")
    return ex, info, base_url


@dataclass
class _Plan:
    side: str          # "BUY" or "SELL"
    coin: str          # e.g., "BTC"
    px: float          # limit price
    sz: float          # size in coin
    tif: str           # "PostOnly" or "Gtc"
    reduceOnly: bool = False


def _make_plan(side: str, symbol: str, entry_low: float, entry_high: float, lev: float | None) -> _Plan:
    # Parse symbol like "BTC/USD" => coin="BTC"
    coin = symbol.split("/")[0].upper()

    # Mid price of the entry band
    mid = (float(entry_low) + float(entry_high)) / 2.0
    px = _round8(mid)

    # Size heuristic:
    # Use a small fixed notional so tests don't fail for precision. You can set HYPER_NOTIONAL_USD.
    notional_usd = float(_env("HYPER_NOTIONAL_USD", "50") or "50")
    # Apply leverage to notional if provided (purely sizing logic, exchange enforces real leverage).
    if lev and lev > 0:
        notional_usd *= float(lev)

    sz = _round8(notional_usd / px)

    tif = (_env("HYPER_DEFAULT_TIF") or "PostOnly").strip()
    tif = "PostOnly" if tif.lower() == "postonly" else "Gtc"

    return _Plan(side=side, coin=coin, px=px, sz=sz, tif=tif)


def _order_type_from_tif(tif: str) -> dict:
    # Use dict form accepted by signing.order_request_to_order_wire
    # PostOnly => {"postOnly": {}}
    # Gtc/Ioc etc. => {"limit": {"tif": "Gtc"}}
    t = tif.strip()
    if t.lower() == "postonly":
        return {"postOnly": {}}
    return {"limit": {"tif": t}}


def _build_order(plan: _Plan) -> dict:
    return {
        "coin": plan.coin,
        "is_buy": True if plan.side.upper() in ("BUY", "LONG") else False,
        "sz": _round8(plan.sz),
        "limit_px": _round8(plan.px),
        "order_type": _order_type_from_tif(plan.tif),
        "reduce_only": bool(plan.reduceOnly),
        # "cloid": None,  # optional client order id; omit for simplicity
    }


def _try_bulk_with_rounding(ex: Exchange, order: dict) -> dict:
    """
    Try bulk_orders; if wire-rounding trips, back off size by 1e-8 a few times.
    """
    # First, ensure floats (not strings)
    order["sz"] = float(order["sz"])
    order["limit_px"] = float(order["limit_px"])

    # Primary attempt
    try:
        return ex.bulk_orders([order])
    except Exception as e:
        last_err = e

    # Backoff attempts on size only, up to 5 times
    step = 1e-8
    for _ in range(5):
        new_sz = max(0.0, float(order["sz"]) - step)
        # If size hits zero, bail
        if new_sz <= 0.0:
            break
        order["sz"] = _round8(new_sz)
        try:
            return ex.bulk_orders([order])
        except Exception as e:
            last_err = e

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")


# --- Public submit function (called by execution.py) --------------------------

def submit_signal(sig) -> None:
    """
    Expected attributes on `sig`:
      - side: "LONG"/"SHORT" (or "BUY"/"SELL")
      - symbol: like "BTC/USD"
      - entry_low, entry_high: floats
      - stop_loss: float (parsed but not used in this simple example)
      - lev: float or None
      - tif: optional, env overrides anyway
    """
    # Validate entry band present
    if getattr(sig, "entry_low", None) is None or getattr(sig, "entry_high", None) is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    side = str(getattr(sig, "side")).upper()
    if side.startswith("LONG") or side.startswith("BUY"):
        side_norm = "BUY"
    elif side.startswith("SHORT") or side.startswith("SELL"):
        side_norm = "SELL"
    else:
        raise ValueError(f"Unsupported side: {getattr(sig, 'side')}")

    symbol = str(getattr(sig, "symbol"))
    allowed = _get_allowed_symbols()
    if allowed is not None:
        if symbol.upper() not in allowed:
            log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
            return

    entry_low = float(getattr(sig, "entry_low"))
    entry_high = float(getattr(sig, "entry_high"))
    sl = getattr(sig, "stop_loss", None)  # parsed but not enforced in this minimal example
    lev = getattr(sig, "lev", None)

    ex, _info, _base = _mk_clients()

    # Plan + log
    plan = _make_plan(side_norm, symbol, entry_low, entry_high, lev)
    log.info("[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=%s",
             side_norm, symbol, entry_low, entry_high, str(sl), str(lev), plan.tif)
    log.info("[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s reduceOnly=%s",
             plan.side, plan.coin, plan.px, plan.sz, plan.tif, plan.reduceOnly)

    order = _build_order(plan)

    # Place order
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders OK: %s", resp)
