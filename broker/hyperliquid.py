# broker/hyperliquid.py
import os
import logging
from dataclasses import dataclass

from hyperliquid.exchange import Exchange, Info    # 0.4.66 layout
from hyperliquid.wallet import Wallet              # 0.4.66 layout

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# ----- Config -----
_ALLOWED = set([s.strip() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()])
_DEFAULT_TIF = os.getenv("HYPER_TIF", "PostOnly")  # HL types: "PostOnly" | "Ioc" | "Alo" | None

# Wallet private key (0x-prefixed hex)
_PRIVKEY = os.getenv("HYPER_PRIVATE_KEY", "").strip()

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
    if not __PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")
    return Wallet(_PRIVKEY)

def _mk_clients() -> tuple[Exchange, Info]:
    """
    0.4.66 API:
        - Wallet: hyperliquid.wallet.Wallet
        - Exchange: hyperliquid.exchange.Exchange(wallet)
        - Info: hyperliquid.exchange.Info()
    """
    w = _require_wallet()
    ex = Exchange(w)
    info = Info()
    return ex, info

def _norm_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    return symbol.split("/")[0].upper()

def _allowed(symbol: str) -> bool:
    return not _ALLOWED or symbol.upper() in _ALLOWED

def submit_signal(sig) -> None:
    """
    Entry point used by execution.py

    sig has:
      side: "LONG"/"SHORT" (we map to BUY/SELL)
      symbol: "BTC/USD"
      entry_low: float
      entry_high: float
      stop_loss: float | None
      leverage: float | None
      tif: str | None (optional)
    """
    if not (sig.entry_low and sig.entry_high):
        raise ValueError("Signal missing entry_band=(low, high).")

    if not _allowed(sig.symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
        return

    side = "BUY" if sig.side.upper() == "LONG" else "SELL"
    coin = _norm_coin(sig.symbol)

    # Mid price for a single post-only limit order
    limit_px = (float(sig.entry_low) + float(sig.entry_high)) / 2.0

    # Simple notional -> size: take $100 notionals per default unless you pass size from outside
    notional = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
    size = notional / limit_px

    plan = ExecPlan(
        side=side,
        coin=coin,
        limit_px=limit_px,
        size=size,
        tif=(sig.tif or _DEFAULT_TIF) if (sig.tif or _DEFAULT_TIF) else None,
        reduce_only=False,
    )

    log.info("[BROKER] BUY/SELL %s/%s band=(%.6f,%.6f) SL=%s lev=%s TIF=%s",
             plan.side, sig.symbol, sig.limit_px, sig.limit_px, sig.stop_loss, sig.leverage, plan.tif)

    _place_limit(plan)

def _place_limit(plan: ExecPlan) -> None:
    ex, _info = _mk_clients()

    # 0.4.66 bulk_orders payload
    order = {
        "coin": plan.coin,
        "is_buy": True if plan.side == "BUY" else False,
        "sz": float(plan.size),
        "limit_px": float(plan.limit_px),
        "order_type": {},  # set TIF below
        "reduce_only": bool(plan.reduce_only),
        "client_id": None,
    }

    if plan.tif and plan.tif.lower() == "postonly":
        order["order_type"] = {"postOnly": {}}
    elif plan.tif and plan.tif.lower() == "ioc":
        order["order_type"] = {"ioc": {}}
    elif plan.tif and plan.tif.lower() == "alo":
        order["order_type"] = {"alo": {}}
    else:
        order["order_type"] = {}  # default plain limit

    log.info("[BROKER] PLAN side=%s coin=%s px=%.8f sz=%.8f tif=%s reduceOnly=%s",
             plan.side, plan.coin, plan.limit_px, plan.size, plan.tif, plan.reduce_only)

    # Call SDK once
    resp = ex.bulk_orders([order])
    log.info("[BROKER] bulk_orders resp: %s", resp)
