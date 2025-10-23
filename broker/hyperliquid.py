# broker/hyperliquid.py
import os
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger("broker.hyperliquid")

# --- SDK imports (0.x friendly) ---
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

# --- env ---
API_KEY = os.getenv("HYPER_API_KEY")
API_SECRET = os.getenv("HYPER_API_SECRET")
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY") or os.getenv("HYPER_EVM_PRIVKEY")
ONLY = [s.strip() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()]
DEFAULT_TIF = os.getenv("HYPER_TIF", "PostOnly")  # PostOnly | Gtc | Ioc | Fok

def _mask(pk: Optional[str]) -> str:
    if not pk or len(pk) < 10:
        return "MISSING"
    return pk[:6] + "…" + pk[-4:]

@dataclass
class Plan:
    coin: str
    is_buy: bool
    px: float
    sz: float
    tif: str
    reduce_only: bool = False

def _mk_clients() -> Tuple[Exchange, Info]:
    """
    Create Exchange/Info compatible with SDK 0.x.
    Tries multiple constructor signatures to match installed version.
    """
    # Try in order: wallet/agent/private_key/no-arg
    tried = []

    # Some 0.x builds accept wallet=... or agent=...
    wallet_kw_names = ("wallet", "agent", "private_key")
    for kw in wallet_kw_names:
        if PRIVKEY:
            try:
                ex = Exchange(**{kw: PRIVKEY})  # type: ignore[arg-type]
                info = Info()
                log.info("[BROKER] Exchange init via %s with HYPER_PRIVATE_KEY=%s", kw, _mask(PRIVKEY))
                return ex, info
            except TypeError as e:
                tried.append(f"{kw}: {e}")

    # Older builds: api_key/secret (API wallet)
    if API_KEY and API_SECRET:
        try:
            ex = Exchange(api_key=API_KEY, secret=API_SECRET)  # type: ignore[arg-type]
            info = Info()
            log.info("[BROKER] Exchange init via api_key/secret.")
            return ex, info
        except TypeError as e:
            tried.append(f"api_key: {e}")

    # Very old builds: no-arg ctor + global Info()
    try:
        ex = Exchange()  # type: ignore[call-arg]
        info = Info()
        log.warning("[BROKER] Exchange init with no args (legacy). HYPER_PRIVATE_KEY=%s", _mask(PRIVKEY))
        return ex, info
    except Exception as e:
        tried.append(f"no-arg: {e}")

    raise RuntimeError("Could not construct Exchange with installed SDK; tried -> " + " | ".join(tried))

def _symbol_ok(symbol: str) -> bool:
    if not ONLY:
        return True
    return symbol in ONLY

def _norm_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    return symbol.split("/")[0].upper().strip()

def _tif_key(tif: str) -> str:
    # Map TIF to wire schema expected by 0.x bulk_orders: {"order_type": {"limit": {"tif": "postOnly"}}}
    t = tif.lower()
    if t in ("postonly", "post_only", "post"):
        return "postOnly"
    if t in ("gtc",):
        return "gtc"
    if t in ("ioc",):
        return "ioc"
    if t in ("fok",):
        return "fok"
    # default
    return "postOnly"

def _build_plan(side: str, symbol: str, entry_low: float, entry_high: float, notional_usd: Optional[float],
                fixed_qty: Optional[float], lev: float, tif: Optional[str]) -> Plan:
    coin = _norm_coin(symbol)
    is_buy = side.upper() == "LONG"
    # midpoint entry
    px = (float(entry_low) + float(entry_high)) / 2.0
    # size: prefer fixed_qty coin size; else notional / px
    if fixed_qty and fixed_qty > 0:
        sz = float(fixed_qty)
    else:
        dollars = float(notional_usd or 100.0)  # fallback $100 notional if unset
        sz = (dollars * float(lev)) / px
    return Plan(coin=coin, is_buy=is_buy, px=px, sz=sz, tif=(tif or DEFAULT_TIF))

def submit_signal(sig) -> None:
    """
    Execute an ExecSignal (from execution.py) using SDK 0.x wire format.
    Expects sig.entry_low, sig.entry_high, sig.symbol, sig.side, sig.stop_loss, etc.
    """
    symbol = getattr(sig, "symbol")
    side = getattr(sig, "side")
    entry_low = getattr(sig, "entry_low", None)
    entry_high = getattr(sig, "entry_high", None)
    stop_loss = getattr(sig, "stop_loss", None)
    lev = float(getattr(sig, "leverage", 1.0))
    tif = getattr(sig, "tif", DEFAULT_TIF)
    notional = getattr(sig, "notional_usd", None)
    fixed_qty = getattr(sig, "fixed_qty", None)

    if not _symbol_ok(symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return
    if entry_low is None or entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    plan = _build_plan(side, symbol, float(entry_low), float(entry_high), notional, fixed_qty, lev, tif)
    log.info("[BROKER] %s %s band=(%.6f,%.6f) SL=%s lev=%.1f TIF=%s",
             side, symbol, float(entry_low), float(entry_high), stop_loss, lev, plan.tif)

    ex, info = _mk_clients()

    # Build legacy 0.x order dict (no OrderType class)
    tif_wire = _tif_key(plan.tif)
    order = {
        "coin": plan.coin,
        "is_buy": bool(plan.is_buy),
        "sz": f"{plan.sz:.8f}",
        "limit_px": f"{plan.px:.8f}",
        "reduce_only": False,
        "order_type": {"limit": {"tif": tif_wire}},
    }

    # Place via bulk_orders (works across 0.x)
    try:
        # builder/cloid omitted; SDK adds them as needed
        resp = ex.bulk_orders([order])
        log.info("[BROKER] bulk_orders resp: %s", resp)
    except Exception as e:
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e
