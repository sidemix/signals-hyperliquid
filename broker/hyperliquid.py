# broker/hyperliquid.py
import os
import logging
from dataclasses import dataclass

from hyperliquid.exchange import Exchange, Info  # SDK 0.4.66
from hyperliquid.wallet import Wallet            # SDK 0.4.66

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# ----- Config -----
_ALLOWED = set(s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip())
_DEFAULT_TIF = os.getenv("HYPER_TIF", "PostOnly")  # "PostOnly" | "Ioc" | "Alo" | None
_PRIVKEY = os.getenv("HYPER_PRIVATE_KEY", "").strip()
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))

# ----- Types your execution layer calls into -----
@dataclass
class ExecPlan:
    side: str            # "BUY" | "SELL"
    coin: str            # e.g. "BTC"
    limit_px: float
    size: float
    tif: str | None
    reduce_only: bool = False


def _require_wallet() -> Wallet:
    if not _PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")
    return Wallet(_PRIVKEY)


def _mk_clients() -> tuple[Exchange, Info]:
    """
    SDK 0.4.66:
      - Wallet:   hyperliquid.wallet.Wallet
      - Exchange: hyperliquid.exchange.Exchange(wallet)
      - Info:     hyperliquid.exchange.Info()
    """
    w = _require_wallet()
    return Exchange(w), Info()


def _coin_from_symbol(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    return (symbol or "").split("/")[0].upper()


def _symbol_ok(symbol: str) -> bool:
    """Allow if no allowlist, or if either COIN or full SYMBOL matches."""
    if not _ALLOWED:
        return True
    sym_up = (symbol or "").upper()
    coin = _coin_from_symbol(symbol)
    return sym_up in _ALLOWED or coin in _ALLOWED


def _order_type_for_tif(tif: str | None) -> dict:
    if not tif:
        return {}
    t = tif.lower()
    if t == "postonly":
        return {"postOnly": {}}
    if t == "ioc":
        return {"ioc": {}}
    if t == "alo":
        return {"alo": {}}
    return {}  # default plain limit


def submit_signal(sig) -> None:
    """
    Entry point used by execution.py

    sig has:
      side: "LONG"/"SHORT"
      symbol: "BTC/USD"
      entry_low: float
      entry_high: float
      stop_loss: float | None
      leverage: float | None
      tif: str | None (optional)
    """
    # Validate inputs early
    if sig is None:
        raise ValueError("submit_signal(sig): sig is None")

    if not (getattr(sig, "entry_low", None) and getattr(sig, "entry_high", None)):
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol = getattr(sig, "symbol", "") or ""
    if not _symbol_ok(symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    side_raw = (getattr(sig, "side", "") or "").upper()
    if side_raw not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side '{sig.side}'. Expected LONG or SHORT.")
    side = "BUY" if side_raw == "LONG" else "SELL"

    coin = _coin_from_symbol(symbol)

    # Mid price for a single post-only limit order
    entry_low = float(sig.entry_low)
    entry_high = float(sig.entry_high)
    limit_px = (entry_low + entry_high) / 2.0

    # Size computed from notional unless external size logic is added later
    notional = float(getattr(sig, "notional_usd", _DEFAULT_NOTIONAL))
    if limit_px <= 0:
        raise ValueError(f"Computed limit_px <= 0 for {symbol}: {limit_px}")
    size = notional / limit_px

    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)

    plan = ExecPlan(
        side=side,
        coin=coin,
        limit_px=limit_px,
        size=size,
        tif=tif,
        reduce_only=False,
    )

    log.info(
        "[BROKER] PLAN side=%s symbol=%s coin=%s band=(%.6f, %.6f) mid=%.6f sz=%.6f SL=%s lev=%s TIF=%s",
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

    log.info(
        "[BROKER] SEND bulk_orders: side=%s coin=%s px=%.8f sz=%.8f tif=%s reduceOnly=%s",
        plan.side, plan.coin, plan.limit_px, plan.size, plan.tif, plan.reduce_only
    )

    resp = ex.bulk_orders([order])
    log.info("[BROKER] bulk_orders resp: %s", resp)
