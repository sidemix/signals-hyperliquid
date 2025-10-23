import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

# Hyperliquid SDK
from hyperliquid.exchange import Exchange  # type: ignore
from hyperliquid.info import Info          # type: ignore

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# === Local Side enum (avoid importing a missing broker.types) ===
class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


# === Dataclasses used by the broker ===
@dataclass
class OrderPlan:
    is_buy: bool
    coin: str
    px: float           # float for SDK
    sz: float           # float for SDK
    tif: str            # e.g., "PostOnly", "Gtc"
    reduce_only: bool = False


# === Utils ===
def _symbol_to_coin(symbol: str) -> str:
    """
    Convert something like 'BTC/USD' or 'btc/usd' -> 'BTC'
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/")[0]
    return s


def _allowed(symbol: str) -> bool:
    """
    Optional allowlist: HYPER_ONLY_EXECUTE_SYMBOLS=CSV of pairs like BTC/USD,ETH/USD
    If not set, allow all.
    """
    allow_csv = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").strip()
    if not allow_csv:
        return True
    allowed = [x.strip().upper() for x in allow_csv.split(",") if x.strip()]
    ok = symbol.strip().upper() in allowed
    log.info(
        "[BROKER] symbol=%s allowed=%s",
        symbol,
        ",".join(allowed) if allowed else "(none)"
    )
    return ok


def _mk_clients() -> Tuple[Exchange, Info]:
    """
    Try a couple of constructor styles so we work across sdk versions.
    Wallet private key auth is the target.
    """
    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    # Try common constructor styles (don’t pass unknown kwargs).
    last_err: Optional[Exception] = None
    for ctor in (
        lambda: Exchange(priv),                     # positional private key
        lambda: Exchange(private_key=priv),         # keyword private key
        lambda: Exchange(wallet=priv),              # older alias
    ):
        try:
            ex = ctor()
            log.info("[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY=%s…%s",
                     priv[:6], priv[-4:])
            info = Info()
            return ex, info
        except Exception as e:  # noqa: BLE001
            last_err = e

    raise RuntimeError(f"Could not initialize Hyperliquid Exchange with provided key: {last_err}")


def _default_tif() -> str:
    """
    TIF comes from env, default 'PostOnly' (good for maker entries).
    Accepts: PostOnly, Gtc, Ioc, Fok (SDK expects exact strings).
    """
    return os.getenv("HYPER_TIF", "PostOnly").strip()


def _usd_per_trade() -> float:
    """
    Sizing in notional USD. Default small for safety. Env: USD_PER_TRADE
    """
    try:
        return float(os.getenv("USD_PER_TRADE", "50"))
    except Exception:
        return 50.0


def _build_plan(
    *,
    side: str,
    symbol: str,
    entry_low: float,
    entry_high: float,
    leverage: Optional[float] = None,
    tif: Optional[str] = None,
) -> OrderPlan:
    """
    Build an order plan from the parsed signal. Keep px/sz as floats.
    """
    coin = _symbol_to_coin(symbol)
    is_buy = side.strip().upper() == Side.LONG
    mid_px = (float(entry_low) + float(entry_high)) / 2.0
    tif = (tif or _default_tif()).strip()

    # very simple sizing: USD_PER_TRADE / mid_px
    usd = _usd_per_trade()
    sz = max(usd / mid_px, 0.0)

    # optional leverage does not affect sz here (you can apply on position if needed)
    plan = OrderPlan(
        is_buy=is_buy,
        coin=coin,
        px=float(mid_px),
        sz=float(sz),
        tif=tif,
        reduce_only=False,
    )

    log.info(
        "[PLAN] side=%s coin=%s px=%.8f sz=%.8f tif=%s reduceOnly=%s",
        "BUY" if is_buy else "SELL", coin, plan.px, plan.sz, plan.tif, plan.reduce_only
    )
    return plan


# === Public API ===
def submit_signal(sig) -> None:
    """
    Bridge from execution.py: sig has fields:
      - side ("LONG"/"SHORT")
      - symbol ("BTC/USD", ...)
      - entry_low, entry_high (floats)
      - stop_loss (float)  [currently not placed as an on-exchange stop]
      - leverage (float|None)
      - tif (optional str)
    """
    # basic checks / allowlist
    if not getattr(sig, "entry_low", None) or not getattr(sig, "entry_high", None):
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol: str = getattr(sig, "symbol")
    if not _allowed(symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    # Build plan
    plan = _build_plan(
        side=str(getattr(sig, "side")),
        symbol=symbol,
        entry_low=float(getattr(sig, "entry_low")),
        entry_high=float(getattr(sig, "entry_high")),
        leverage=float(getattr(sig, "leverage")) if getattr(sig, "leverage", None) is not None else None,
        tif=str(getattr(sig, "tif")) if getattr(sig, "tif", None) else None,
    )

    log.info(
        "[BROKER] %s %s band=(%.6f,%.6f) SL=%s lev=%s TIF=%s",
        plan.is_buy and "LONG" or "SHORT",
        symbol,
        float(getattr(sig, "entry_low")),
        float(getattr(sig, "entry_high")),
        str(getattr(sig, "stop_loss", "n/a")),
        str(getattr(sig, "leverage", "n/a")),
        plan.tif,
    )

    # Prepare SDK clients
    ex, _ = _mk_clients()

    # Build the order payload for the SDK. Keep floats for limit_px & sz.
    order = {
        "coin": plan.coin,
        "is_buy": bool(plan.is_buy),
        "sz": float(plan.sz),
        "limit_px": float(plan.px),
        "order_type": {"limit": {"tif": plan.tif}},  # dict form is SDK-compatible
        "reduce_only": bool(plan.reduce_only),
        # You can add "cloid" here if you want client order IDs
    }

    # Place the order
    try:
        resp = ex.bulk_orders([order])
        log.info("[BROKER] bulk_orders response: %s", str(resp))
    except Exception as e:  # noqa: BLE001
        # The SDK error you saw ("Unknown format code 'f' ...") is caused by passing strings for floats.
        # We're passing floats now, so if this triggers again, it’s a different issue (log it).
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e
