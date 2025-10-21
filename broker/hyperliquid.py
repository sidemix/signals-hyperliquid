# broker/hyperliquid.py
"""
Hyperliquid broker shim used by execution.execute_signal().

This version uses the **official Hyperliquid Python SDK** to place orders.
You get: entry order + reduce-only TPs + reduce-only stop (OTO-style).

ENV you should have:
- HYPER_API_KEY          (if your SDK setup uses it; some SDK flows only need PRIVATE_KEY)
- HYPER_API_SECRET       (optional; depends on your SDK setup)
- HYPER_PRIVATE_KEY      (private key string used by SDK signer)
- HYPER_BASE_URL         (default: https://api.hyperliquid.xyz)
- ACCOUNT_MODE           ("perp" or "spot"; default "perp")
- TRADE_SIZE_USD         (float; e.g. 100)
- DRY_RUN                ("true"/"false")

We accept payload with keys:
  symbol, side, entry_band(tuple[low, high]), stop, tps(list), leverage?, timeframe?
…and place:
  1) limit entry at the **mid of the band**
  2) multiple reduce-only TP limits (laddered)
  3) one reduce-only stop trigger

If your SDK method names differ, tweak the calls in _hl_place_limit/_hl_place_trigger.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# ---------- Small utilities ----------

def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "1" if default else "0")).strip().lower() in ("1","true","yes","on")

def _get_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _log(msg: str) -> None:
    print(msg, flush=True)

def _norm_symbol(sym: str) -> str:
    # "ETH/USD" -> "ETH" (HL perps are coin-USD notation)
    s = sym.strip().upper()
    if s.endswith("/USD"):
        return s[:-4]
    if s.endswith("-USD"):
        return s[:-4]
    return s

def _account_mode() -> str:
    return (os.getenv("ACCOUNT_MODE", "perp") or "perp").lower()

# ---------- “SDK” bootstrap (lazy import so your service can still boot in DRY_RUN) ----------

def _load_sdk_exchange():
    """
    Import and initialize the Hyperliquid SDK Exchange client.
    Adjust to the SDK you installed.

    pip install hyperliquid-python-sdk
      -> from hyperliquid import Exchange

    If your SDK uses a different module name, change it here.
    """
    # You might need: pip install git+https://github.com/hyperliquid-dex/hyperliquid-python-sdk
    from hyperliquid import Exchange  # type: ignore

    base_url = os.getenv("HYPER_BASE_URL", "https://api.hyperliquid.xyz")
    private_key = os.getenv("HYPER_PRIVATE_KEY", "")
    if not private_key:
        raise RuntimeError("HYPER_PRIVATE_KEY not set")

    # Most SDKs allow something like:
    ex = Exchange(private_key=private_key, base_url=base_url, account_mode=_account_mode())
    return ex

# ---------- Sizing helpers ----------

def _calc_size_usd_to_coin(usd: float, mark: float) -> float:
    # notional / price -> coin size
    if mark <= 0:
        raise ValueError("Invalid mark price for sizing.")
    return round(usd / mark, 6)  # 6 dp is usually more than enough; tweak per market rules

# ---------- Normalized execution request ----------

@dataclass
class ExecPayload:
    symbol: str            # e.g. "ETH/USD"
    side: str              # "LONG"/"SHORT"
    entry_band: Tuple[float, float]
    stop: float
    tps: List[float]
    leverage: float | None = None
    timeframe: str | None = None

def _normalize_payload(obj: Dict[str, Any]) -> ExecPayload:
    # Expect same dict your execution.py passes
    sym = str(obj["symbol"]).upper()
    side = str(obj["side"]).upper()
    band = obj["entry_band"]
    stop = float(obj["stop"])
    tps  = [float(x) for x in obj.get("tps", [])]
    lev  = obj.get("leverage")
    tf   = obj.get("timeframe")
    return ExecPayload(
        symbol=sym, side=side, entry_band=(float(band[0]), float(band[1])),
        stop=stop, tps=tps, leverage=lev, timeframe=tf
    )

# ---------- SDK order wrappers ----------
#
# NOTE: The exact SDK function names / params can vary slightly by version.
# If your SDK exposes different names, edit here only — the rest of your stack stays the same.

def _hl_place_limit(ex, coin: str, is_buy: bool, size: float, price: float, reduce_only: bool = False):
    """
    Place a **limit** order via SDK.
    Adjust this call to match the SDK you installed.
    Example signatures seen in SDKs:
        ex.place_order(coin=coin, is_buy=is_buy, size=size, limit_price=price,
                       reduce_only=reduce_only, tif="Gtc")
    """
    _log(f"[HL] place_limit coin={coin} side={'BUY' if is_buy else 'SELL'} "
         f"size={size} price={price} reduce_only={reduce_only}")
    # --- EDIT BELOW to your SDK’s actual call signature ---
    return ex.place_order(
        coin=coin,
        is_buy=is_buy,
        size=size,
        limit_price=price,
        reduce_only=reduce_only,
        tif="Gtc"       # Good-til-cancel; change if you prefer PostOnly/Ioc if SDK supports it
    )

def _hl_place_trigger(ex, coin: str, is_buy: bool, size: float, trigger_price: float,
                      reduce_only: bool = True, is_stop: bool = True):
    """
    Place a **trigger** order (stop or take-profit) via SDK.
    Some SDKs expose:
        ex.place_trigger(coin=..., is_buy=..., size=..., trigger_price=..., is_stop=is_stop, reduce_only=True)
    Others might call it 'place_conditional' or similar.
    """
    _log(f"[HL] place_trigger coin={coin} side={'BUY' if is_buy else 'SELL'} "
         f"size={size} trigger={trigger_price} reduce_only={reduce_only} is_stop={is_stop}")
    # --- EDIT BELOW to your SDK’s actual call signature ---
    return ex.place_trigger(
        coin=coin,
        is_buy=is_buy,
        size=size,
        trigger_price=trigger_price,
        reduce_only=reduce_only,
        is_stop=is_stop,   # True => stop, False => take-profit trigger
        tif="Gtc"
    )

def _hl_mark_price(ex, coin: str) -> float:
    """
    Get the latest mark (or mid) price. SDK usually exposes a ticker or mark endpoint.
    """
    # Example pattern:
    #   px = ex.get_mark_price(coin)  # tailor to SDK
    ticker = ex.get_ticker(coin)      # many SDKs return dict with 'mark' or 'last'
    px = float(ticker.get("mark") or ticker.get("last") or ticker["price"])
    return px

# ---------- Public entry from execution.py ----------

def submit_signal(sig_or_kwargs: Dict[str, Any], **kw) -> None:
    """
    Called by execution.execute_signal().
    Accepts either an ExecSignal (dataclass) or plain kwargs. We expect a dict here from the caller.
    """
    payload = sig_or_kwargs if kw == {} else {**sig_or_kwargs, **kw}
    pl = _normalize_payload(payload)

    # Pretty banner
    _log(
        "[BROKER] "
        f"{pl.side} {pl.symbol} band=({pl.entry_band[0]:.6f},{pl.entry_band[1]:.6f}) "
        f"SL={pl.stop:.6f} TPn={len(pl.tps)} lev={pl.leverage or 'n/a'} TF={pl.timeframe or 'n/a'}"
    )

    # DRY-RUN?
    if _env_bool("DRY_RUN", False):
        _log("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    # Init SDK
    ex = _load_sdk_exchange()

    # Convert "ETH/USD" -> "ETH"
    coin = _norm_symbol(pl.symbol)

    # Get sizing
    trade_usd = _get_env_float("TRADE_SIZE_USD", 100.0)
    mark = _hl_mark_price(ex, coin)
    size = _calc_size_usd_to_coin(trade_usd, mark)
    is_buy_entry = (pl.side == "LONG")

    # 1) Entry: pick the mid of the band as the working limit price
    entry_price = round((pl.entry_band[0] + pl.entry_band[1]) / 2.0, 4)
    _hl_place_limit(ex, coin, is_buy=is_buy_entry, size=size, price=entry_price, reduce_only=False)

    # 2) Laddered TPs (reduce-only). If LONG, TPs are sells above; if SHORT, TPs are buys below.
    # Use equal splits; feel free to change this logic.
    if pl.tps:
        tp_each = round(size / len(pl.tps), 6)
        for px in pl.tps:
            # take-profit triggers are "opposite side" and reduce-only
            is_buy_tp = not is_buy_entry
            # Some SDKs have dedicated take-profit trigger; if not, use trigger with is_stop=False
            _hl_place_trigger(ex, coin, is_buy=is_buy_tp, size=tp_each, trigger_price=float(px),
                              reduce_only=True, is_stop=False)

    # 3) Stop (reduce-only). For LONG, stop is a sell trigger; for SHORT, stop is a buy trigger.
    is_buy_stop = not is_buy_entry
    _hl_place_trigger(ex, coin, is_buy=is_buy_stop, size=size, trigger_price=float(pl.stop),
                      reduce_only=True, is_stop=True)

    _log(f"[BROKER] submitted {pl.side} {pl.symbol} @ {entry_price} with {len(pl.tps)} TP(s) + 1 SL")
