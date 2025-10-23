"""
Broker adapter for Hyperliquid (SDK v1.1.7).
- Single SDK only: `hyperliquid==1.1.7`
- Wallet auth: Exchange(privkey=HYPER_PRIVATE_KEY)
- Post-only via TIF='Alo' (Add Liquidity Only)
- Pre-quantizes price/size to 8dp to avoid float_to_wire rounding errors
"""

from __future__ import annotations
import os
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Tuple

from hyperliquid import Exchange, Info  # SDK 1.1.7

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ---------- Config ----------
HYPER_PRIVATE_KEY = os.getenv("HYPER_PRIVATE_KEY")  # required
DEFAULT_USD_NOTIONAL = float(os.getenv("HYPER_USD_NOTIONAL", "50"))  # $ notional per signal
# Comma-separated list of "COIN/USD" to allow; empty = allow any
ONLY_EXECUTE = set(
    s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()
)

# TIF to use for entry orders: "Alo" = post-only, "Gtc" = good-till-cancel, "Ioc" = immediate-or-cancel
ENTRY_TIF = os.getenv("HYPER_ENTRY_TIF", "Alo")


# ---------- Utilities ----------
def _require_privkey() -> str:
    if not HYPER_PRIVATE_KEY:
        raise RuntimeError("HYPER_PRIVATE_KEY is not set.")
    return HYPER_PRIVATE_KEY


def _mk_clients() -> Tuple[Exchange, Info]:
    """Create SDK clients using wallet private key (string)."""
    priv = _require_privkey()
    # SDK 1.1.7 takes `privkey` and constructs the wallet internally
    ex = Exchange(privkey=priv)
    info = Info()
    log.info("[BROKER] Exchange init via wallet (privkey=*****)")
    return ex, info


def _quant8(x: float) -> float:
    """
    Quantize to 8 decimal places (no rounding up) so SDK's float_to_wire
    won't need to round (which would raise).
    """
    d = Decimal(str(x)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return float(d)


def _parse_symbol_coin(symbol_sig: str) -> str:
    """
    Convert 'BTC/USD' -> 'BTC' (SDK expects coin ticker without '/USD').
    """
    sym = (symbol_sig or "").strip().upper()
    if "/" in sym:
        return sym.split("/")[0]
    return sym


def _want_to_trade(symbol_sig: str) -> bool:
    if not ONLY_EXECUTE:
        return True
    return symbol_sig.upper() in ONLY_EXECUTE


def _side_is_buy(side_sig: str) -> bool:
    s = (side_sig or "").strip().upper()
    # Accept LONG/BUY for buy, SHORT/SELL for sell
    return s in ("LONG", "BUY")


def _mid(a: float, b: float) -> float:
    return (float(a) + float(b)) / 2.0


def _build_limit_order(coin: str, is_buy: bool, sz: float, limit_px: float, tif: str) -> Dict[str, Any]:
    """
    Order request format expected by hyperliquid==1.1.7:
    {
      "coin": "BTC",
      "is_buy": true,
      "sz": 0.00123456,
      "limit_px": 109525.0,
      "order_type": {"limit": {"tif": "Gtc"}},  # or "Alo" for post-only
      "reduce_only": false
    }
    """
    if tif not in ("Alo", "Gtc", "Ioc"):
        tif = "Gtc"
    return {
        "coin": coin,
        "is_buy": bool(is_buy),
        "sz": _quant8(sz),
        "limit_px": _quant8(limit_px),
        "order_type": {"limit": {"tif": tif}},
        "reduce_only": False,
    }


def _usd_notional_to_sz(usd_notional: float, px: float) -> float:
    """
    Convert USD notional to coin size.
    (Hyperliquid isolates leverage separately; here we size on notional only.)
    """
    if px <= 0:
        raise ValueError("Invalid price for sizing.")
    return usd_notional / px


def _try_bulk_orders_with_safe_rounding(ex: Exchange, order: Dict[str, Any]) -> Any:
    """
    Try bulk_orders; if float_to_wire complains about rounding, progressively
    shrink size by 1 sat (1e-8) until it succeeds (up to a few steps).
    """
    last_err: Exception | None = None
    for _ in range(10):
        try:
            # Ensure we pass values that never trigger SDK rounding
            order = {
                **order,
                "sz": _quant8(order["sz"]),
                "limit_px": _quant8(order["limit_px"]),
            }
            return ex.bulk_orders([order])
        except Exception as e:  # noqa
            msg = str(e)
            last_err = e
            # Typical SDK rounding errors:
            #  - "float_to_wire causes rounding"
            #  - size too precise
            #  - or downstream signing/encode if wallet not constructed properly
            if "float_to_wire causes rounding" in msg or "Unknown format code 'f'" in msg:
                # reduce size by 1e-8 and retry
                order["sz"] = max(0.0, _quant8(order["sz"] - 1e-8))
                continue
            # if it's not a rounding complaint, bail
            break
    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")


# ---------- Public entrypoint ----------
def submit_signal(sig: Any) -> None:
    """
    Execute a simple band entry as a single post-only (Alo) limit order at band mid.

    Expected attributes on `sig`:
      - side: "LONG"/"SHORT" or "BUY"/"SELL"
      - symbol: like "BTC/USD"
      - entry_low, entry_high: floats
      - stop_loss: float (optional)
      - leverage: float (optional)

    We size using USD notional (env HYPER_USD_NOTIONAL, default $50).
    """
    # Pull attributes safely whether `sig` is a dataclass, pydantic model, or dict-like
    def g(name: str, default=None):
        return getattr(sig, name, getattr(sig, name.replace("-", "_"), default))

    side_sig = g("side")
    symbol_sig = g("symbol")
    low = g("entry_low", g("band_low", None))
    high = g("entry_high", g("band_high", None))
    sl = g("stop_loss", g("sl", None))
    lev = g("leverage", g("lev", None))

    # Basic validation
    if not side_sig or not symbol_sig:
        raise ValueError("Signal missing side and/or symbol.")
    if low is None or high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol_sig = str(symbol_sig).upper()
    if not _want_to_trade(symbol_sig):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol_sig)
        return

    coin = _parse_symbol_coin(symbol_sig)
    is_buy = _side_is_buy(side_sig)
    px = _mid(float(low), float(high))
    px = _quant8(px)  # ensure ≤8dp

    # Size on USD notional (ignore leverage for order sizing; leverage is controlled on exchange)
    usd_notional = DEFAULT_USD_NOTIONAL
    sz = _usd_notional_to_sz(usd_notional, px)
    sz = _quant8(sz)

    log.info(
        "[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=%s",
        "BUY" if is_buy else "SELL",
        symbol_sig,
        float(low),
        float(high),
        sl if sl is not None else "n/a",
        lev if lev is not None else "n/a",
        ENTRY_TIF,
    )
    log.info("[BROKER] PLAN side=%s coin=%s px=%.8f sz=%.8f tif=%s reduceOnly=%s",
             "BUY" if is_buy else "SELL", coin, px, sz, ENTRY_TIF, False)

    ex, _info = _mk_clients()

    order = _build_limit_order(
        coin=coin,
        is_buy=is_buy,
        sz=sz,
        limit_px=px,
        tif=ENTRY_TIF,  # 'Alo' = post-only
    )

    resp = _try_bulk_orders_with_safe_rounding(ex, order)
    log.info("[BROKER] bulk_orders OK: %s", resp)
