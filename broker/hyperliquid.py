# broker/hyperliquid.py
import logging
import os
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple, Callable

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)
log.info("[BROKER] hyperliquid.py loaded")

# ---- HL SDK imports (lazy-compatible) ----
# We import inside helper(s) to survive older / newer SDK shapes.
def _import_sdk():
    """
    Returns (Exchange, Info, order_request_to_order_wire?) as available.
    """
    # Newer SDK location
    try:
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.info import Info  # type: ignore
        return Exchange, Info
    except Exception:
        # Older flat import shapes (fallback)
        from hyperliquid import exchange as _ex  # type: ignore
        from hyperliquid import info as _info  # type: ignore
        Exchange = getattr(_ex, "Exchange")
        Info = getattr(_info, "Info")
        return Exchange, Info


# ---- Config ----
DEFAULT_TIF = os.getenv("HYPER_DEFAULT_TIF", "PostOnly")
BASE_USD = float(os.getenv("HYPER_BASE_USD", "50"))

_ALLOWED = {
    s.strip()
    for s in os.getenv(
        "HYPER_ONLY_EXECUTE_SYMBOLS",
        "ETH/USD,BTC/USD,SOL/USD,LINK/USD,BNB/USD,AVAX/USD",
    ).split(",")
    if s.strip()
}
if "" in _ALLOWED:
    _ALLOWED.remove("")


def _allowed(symbol: str) -> bool:
    ok = symbol in _ALLOWED
    if not ok:
        log.info(
            f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}"
        )
    else:
        log.info(
            f"[BROKER] symbol={symbol} allowed={','.join(sorted(_ALLOWED))}"
        )
    return ok


# ---- Utilities ----
def _split_symbol(sym: str) -> str:
    # SDK expects just coin name (e.g., "BTC") instead of "BTC/USD"
    return sym.split("/")[0].strip().upper()


def _mid(a: float, b: float) -> float:
    return (float(a) + float(b)) / 2.0


def _dec_round_down(x: float, places: int) -> float:
    q = Decimal(10) ** -places
    return float((Decimal(str(x))).quantize(q, rounding=ROUND_DOWN))


def _trim_size_for_sdk(sz: float) -> float:
    """
    HL signer rejects values that would round at wire step.
    We progressively reduce precision to avoid (“float_to_wire causes rounding”).
    """
    for p in (8, 7, 6, 5, 4):
        v = _dec_round_down(sz, p)
        if v > 0:
            return v
    return max(1e-8, sz)


def _extract_entry_band(sig: Any) -> Tuple[float, float]:
    """
    Accept any of:
      - sig.band_low/sig.band_high
      - sig.entry_low/sig.entry_high
      - sig.band == (low, high)
    """
    low = getattr(sig, "band_low", None)
    high = getattr(sig, "band_high", None)

    if low is None or high is None:
        band = getattr(sig, "band", None)
        if isinstance(band, (tuple, list)) and len(band) == 2:
            low, high = band[0], band[1]

    if low is None or high is None:
        low = getattr(sig, "entry_low", low)
        high = getattr(sig, "entry_high", high)

    if low is None or high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    return float(low), float(high)


def _order_dict(coin: str, is_buy: bool, sz: float, px: float, tif: str) -> Dict[str, Any]:
    """
    Build a modern SDK-compatible order dict.
    The SDK (signing.py) expects floats for 'sz' and 'limit_px' and an
    'order_type' dict with tif.
    """
    tif_norm = tif if tif in ("Gtc", "Ioc", "PostOnly") else DEFAULT_TIF
    return {
        "coin": coin,
        "is_buy": bool(is_buy),
        "sz": float(sz),
        "limit_px": float(px),
        "order_type": {"limit": {"tif": tif_norm}},
        "reduce_only": False,
    }


def _try_bulk_with_rounding(ex: Any, order: Dict[str, Any]) -> Any:
    """
    Retry bulk_orders while trimming size precision to avoid float_to_wire rounding errors.
    """
    last_err: Optional[Exception] = None
    base_sz = float(order["sz"])
    for places in (8, 7, 6, 5, 4):
        try:
            safe_sz = _dec_round_down(base_sz, places)
            if safe_sz <= 0:
                continue
            order_mod = dict(order)
            order_mod["sz"] = safe_sz
            return ex.bulk_orders([order_mod])
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(
        f"SDK bulk_orders failed after rounding attempts: {last_err}"
    )


def _mk_clients() -> Tuple[Any, Any]:
    """
    Instantiate Exchange/Info for multiple SDKs:
      - best-effort with raw private key (preferred)
      - finally plain constructors if supported
    """
    Exchange, Info = _import_sdk()

    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError(
            "No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key)."
        )
    if not priv.startswith("0x"):
        priv = "0x" + priv

    # Attempt styles in order (many SDK versions exist in the wild).
    attempts: Tuple[Tuple[str, Callable[[], Any]], ...] = (
        ("Exchange(private_key=...)", lambda: Exchange(private_key=priv)),           # newer
        ("Exchange(priv=...)",         lambda: Exchange(priv=priv)),                 # alt kw
        ("Exchange(privkey=...)",      lambda: Exchange(privkey=priv)),              # alt kw
        ("Exchange(address, priv)",    lambda: Exchange("", priv)),                  # positional legacy (host param ignored)
        ("Exchange()",                 lambda: Exchange()),                          # some forks default to env signer
    )

    ex = None
    last_err: Optional[Exception] = None
    for label, ctor in attempts:
        try:
            ex = ctor()
            log.info(f"[BROKER] Exchange init via private key style: {label}")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if ex is None:
        raise RuntimeError(f"Could not construct Exchange with any style: {last_err}")

    try:
        info = Info()
    except Exception:
        # some SDKs require no Info; create a small shim
        class _NoInfo:
            def __getattr__(self, _name):  # pragma: no cover
                raise AttributeError("Info API not available in this SDK build.")
        info = _NoInfo()

    return ex, info


# ---- Public entrypoint called by execution.execute_signal() ----
def submit_signal(sig: Any) -> None:
    """
    Execute a single 'entry' order from a parsed/normalized signal object.
    Required attributes:
      sig.side           -> "LONG" or "SHORT"
      sig.symbol         -> "BTC/USD", ...
    Any of:
      (sig.band_low, sig.band_high) OR (sig.entry_low, sig.entry_high) OR sig.band=(low, high)

    Optional:
      sig.tif            -> "PostOnly"|"Gtc"|"Ioc"  (default: env HYPER_DEFAULT_TIF or PostOnly)
      sig.base_usd       -> float notional to size position (default: env HYPER_BASE_USD)
      sig.leverage, sig.stop_loss -> currently logged only (no TP/SL automation in this stub)
    """
    side = getattr(sig, "side", None)
    symbol = getattr(sig, "symbol", None)

    if not side or not symbol:
        raise ValueError("Signal missing side and/or symbol.")

    if not _allowed(symbol):
        return

    band_low, band_high = _extract_entry_band(sig)
    tif = getattr(sig, "tif", None) or DEFAULT_TIF

    is_buy = str(side).upper() == "LONG"
    coin = _split_symbol(symbol)

    # Plan mid entry
    mid = _mid(band_low, band_high)
    base_usd = float(getattr(sig, "base_usd", BASE_USD))
    px_safe = max(1e-9, float(mid))
    raw_sz = base_usd / px_safe
    sz = _trim_size_for_sdk(raw_sz)

    log.info(
        f"[BROKER] {side} {symbol} band=({band_low:.6f},{band_high:.6f}) "
        f"SL={getattr(sig, 'stop_loss', None)} lev={getattr(sig, 'leverage', None)} TIF={tif}"
    )
    log.info(
        f"[BROKER] PLAN side={'BUY' if is_buy else 'SELL'} coin={coin} "
        f"px={float(mid):.8f} sz={sz} tif={tif} reduceOnly=False"
    )

    order = _order_dict(coin=coin, is_buy=is_buy, sz=sz, px=mid, tif=tif)

    ex, _info = _mk_clients()

    try:
        resp = _try_bulk_with_rounding(ex, order)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e

    log.info(f"[BROKER] order response: {resp}")
