# broker/hyperliquid.py
import os
import logging
from dataclasses import dataclass

# SDK 0.20.0 import layout
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.wallet import Wallet

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# ----- Config -----
_ALLOWED = set(s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip())
_DEFAULT_TIF = (os.getenv("HYPER_TIF", "Alo") or "").strip()  # Alo | Ioc | Gtc (PostOnly ~= Alo)
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))

@dataclass
class ExecPlan:
    side: str            # "BUY" | "SELL"
    coin: str            # e.g. "BTC"
    limit_px: float
    size: float
    tif: str | None
    reduce_only: bool = False

# ----- Helpers -----
def _require_wallet() -> Wallet:
    if not _PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")
    return Wallet(_PRIVKEY)

def _mk_clients() -> tuple[Exchange, Info]:
    w = _require_wallet()
    return Exchange(w), Info()

def _coin_from_symbol(symbol: str) -> str:
    return (symbol or "").split("/")[0].upper()

def _symbol_ok(symbol: str) -> bool:
    if not _ALLOWED:
        return True
    sym_up = (symbol or "").upper()
    coin = _coin_from_symbol(symbol)
    return sym_up in _ALLOWED or coin in _ALLOWED

def _order_type_for_tif(tif: str | None) -> dict:
    """SDK 0.20.0 expects: {"limit": {"tif": "Alo"|"Ioc"|"Gtc"}} or {} for plain limit."""
    if not tif:
        return {}
    t = tif.strip().lower()
    if t in ("postonly", "alo"):
        return {"limit": {"tif": "Alo"}}
    if t == "ioc":
        return {"limit": {"tif": "Ioc"}}
    if t == "gtc":
        return {"limit": {"tif": "Gtc"}}
    return {}

# ----- Entry point called by execution.py -----
def submit_signal(sig) -> None:
    """
    sig:
      side: "LONG"/"SHORT"
      symbol: "BTC/USD"
      entry_low: float
      entry_high: float
      stop_loss: float | None
      leverage: float | None
      tif: str | None
    """
    if sig is None:
        raise ValueError("submit_signal(sig): sig is None")

    if not (getattr(sig, "entry_low", None) and getattr(sig, "entry_high", None)):
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol = getattr(sig, "symbol", "") or ""
    if not _symbol_ok(symbol):
        log.info("[HL] SKIP: %s not in HYPER_ONLY_EXECUTE_SYMBOLS=%s", symbol, sorted(_ALLOWED))
        return

    side_raw = (getattr(sig, "side", "") or "").upper()
    if side_raw not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side '{sig.side}'. Expected LONG or SHORT.")
    side = "BUY" if side_raw == "LONG" else "SELL"

    coin = _coin_from_symbol(symbol)
    entry_low = float(sig.entry_low)
    entry_high = float(sig.entry_high)
    limit_px = (entry_low + entry_high) / 2.0
    if limit_px <= 0:
        raise ValueError(f"Computed limit_px <= 0 for {symbol}: {limit_px}")

    notional = float(getattr(sig, "notional_usd", _DEFAULT_NOTIONAL))
    size = notional / limit_px

    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)

    plan = ExecPlan(
        side=side, coin=coin, limit_px=limit_px, size=size, tif=tif, reduce_only=False
    )

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.2f, %.2f) mid=%.2f sz=%.4f SL=%s lev=%s TIF=%s",
        plan.side, symbol, coin, entry_low, entry_high, plan.limit_px, plan.size,
        getattr(sig, "stop_loss", None), getattr(sig, "leverage", None), plan.tif
    )

    _place_limit(plan)

def _place_limit(plan: ExecPlan) -> None:
    ex, _info = _mk_clients()
    order = {
        "coin": plan.coin,
        "is_buy": (plan.side == "BUY"),
        "sz": float(plan.size),
        "limit_px": float(plan.limit_px),
        "order_type": _order_type_for_tif(plan.tif),
        "reduce_only": bool(plan.reduce_only),
        "client_id": None,
    }
    log.info("[HL] SEND bulk_orders: %s", order)
    resp = ex.bulk_orders([order])
    log.info("[HL] bulk_orders resp: %s", resp)
