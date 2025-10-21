# broker/hyperliquid.py
"""
Hyperliquid broker shim called by execution.execute_signal().

- Accepts either ExecSignal OR dict payload (we normalize it).
- Uses Hyperliquid Python SDK (recommended) to place:
    1) entry LIMIT on mid of band
    2) reduce-only stop trigger
    3) reduce-only take-profit triggers (laddered)

ENV (Render → Environment):
  HYPER_PRIVATE_KEY=...          # required by SDK signer
  HYPER_BASE_URL=https://api.hyperliquid.xyz
  ACCOUNT_MODE=perp              # or 'spot' if you later support it
  TRADE_SIZE_USD=10              # risk per trade in USD notionals
  DRY_RUN=false                  # true = preview only

  Optional allowlists handled in execution.py:
  AUTHOR_ALLOWLIST=tylerdefi
  HYPER_ONLY_EXECUTE_SYMBOLS=BTC,ETH,SOL
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union

# ---------- utilities ----------
def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "1" if default else "0")).strip().lower() in ("1","true","yes","on")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _log(msg: str) -> None:
    print(msg, flush=True)

def _norm_coin(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith("/USD"):
        return s[:-4]
    if s.endswith("-USD"):
        return s[:-4]
    return s

# ---------- input normalization ----------
@dataclass
class _ExecPayload:
    symbol: str
    side: str
    entry_band: Tuple[float, float]
    stop: float
    tps: List[float]
    leverage: float | None = None
    timeframe: str | None = None

def _normalize_payload(sig_or_dict: Union["_ExecPayload", Dict[str, Any]]) -> _ExecPayload:
    if isinstance(sig_or_dict, dict):
        sym = str(sig_or_dict["symbol"]).upper()
        side = str(sig_or_dict["side"]).upper()
        band = sig_or_dict["entry_band"]
        stop = float(sig_or_dict["stop"])
        tps  = [float(x) for x in sig_or_dict.get("tps", [])]
        lev  = sig_or_dict.get("leverage")
        tf   = sig_or_dict.get("timeframe")
        return _ExecPayload(
            symbol=sym,
            side=side,
            entry_band=(float(band[0]), float(band[1])),
            stop=stop,
            tps=tps,
            leverage=lev,
            timeframe=tf,
        )
    else:
        return sig_or_dict

# ---------- SDK wiring ----------
def _load_sdk():
    """
    Import and build the Hyperliquid SDK exchange client.
    If your SDK module path is different, edit here.
    """
    # pip install hyperliquid-python-sdk   (or the official repo URL)
    from hyperliquid import Exchange  # type: ignore

    base_url = os.getenv("HYPER_BASE_URL", "https://api.hyperliquid.xyz")
    private_key = os.getenv("HYPER_PRIVATE_KEY", "")
    if not private_key:
        raise RuntimeError("HYPER_PRIVATE_KEY not set. Add it in Render → Environment.")
    account_mode = (os.getenv("ACCOUNT_MODE", "perp") or "perp").lower()
    return Exchange(private_key=private_key, base_url=base_url, account_mode=account_mode)

def _mark_price(ex, coin: str) -> float:
    """
    Ask SDK for mark/last price. Adjust to your SDK’s method names.
    """
    t = ex.get_ticker(coin)  # many SDKs expose dict with 'mark' or 'last'
    return float(t.get("mark") or t.get("last") or t.get("price"))

def _place_limit(ex, coin: str, is_buy: bool, size: float, price: float, reduce_only: bool):
    _log(f"[HL] place_limit coin={coin} side={'BUY' if is_buy else 'SELL'} size={size} price={price} RO={reduce_only}")
    # Edit the call below if your SDK uses different arg names:
    return ex.place_order(
        coin=coin,
        is_buy=is_buy,
        size=size,
        limit_price=price,
        reduce_only=reduce_only,
        tif="Gtc",
    )

def _place_trigger(ex, coin: str, is_buy: bool, size: float, trigger_price: float, *, is_stop: bool, reduce_only: bool):
    _log(f"[HL] place_trigger coin={coin} side={'BUY' if is_buy else 'SELL'} size={size} trigger={trigger_price} "
         f"is_stop={is_stop} RO={reduce_only}")
    # If your SDK uses 'place_conditional' or different param names, adapt here:
    return ex.place_trigger(
        coin=coin,
        is_buy=is_buy,
        size=size,
        trigger_price=trigger_price,
        reduce_only=reduce_only,
        is_stop=is_stop,
        tif="Gtc",
    )

# ---------- math ----------
def _size_usd_to_coin(usd: float, price: float) -> float:
    if price <= 0:
        raise RuntimeError("Invalid mark price from exchange.")
    # keep 6 dp; adjust if your markets require stricter increments
    return round(usd / price, 6)

# ---------- public entry (called by execution.execute_signal) ----------
def submit_signal(sig_or_kwargs: Union[_ExecPayload, Dict[str, Any]], **kw) -> None:
    """
    Main entry. Accepts a dict (recommended) or an _ExecPayload.
    """
    # Merge kwargs if someone calls submit_signal(payload, extra=...) (we don’t use extras now)
    payload = sig_or_kwargs if not kw else {**sig_or_kwargs, **kw}  # type: ignore
    pl = _normalize_payload(payload)

    _log(
        "[BROKER] "
        f"{pl.side} {pl.symbol} band=({pl.entry_band[0]:.6f},{pl.entry_band[1]:.6f}) "
        f"SL={pl.stop:.6f} TPn={len(pl.tps)} lev={pl.leverage or 'n/a'} TF={pl.timeframe or 'n/a'}"
    )

    if _env_bool("DRY_RUN", False):
        _log("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    ex = _load_sdk()
    coin = _norm_coin(pl.symbol)

    # Size from USD
    trade_usd = _env_float("TRADE_SIZE_USD", 10.0)
    mark = _mark_price(ex, coin)
    size = _size_usd_to_coin(trade_usd, mark)

    is_buy_entry = (pl.side.upper() == "LONG")
    entry_price = round((pl.entry_band[0] + pl.entry_band[1]) / 2.0, 4)

    # 1) Entry
    _place_limit(ex, coin, is_buy=is_buy_entry, size=size, price=entry_price, reduce_only=False)

    # 2) Take-profits (reduce-only, opposite side), equal split
    if pl.tps:
        tp_each = round(size / len(pl.tps), 6)
        for px in pl.tps:
            _place_trigger(
                ex, coin,
                is_buy=not is_buy_entry,
                size=tp_each,
                trigger_price=float(px),
                is_stop=False,
                reduce_only=True,
            )

    # 3) Stop (reduce-only, opposite side)
    _place_trigger(
        ex, coin,
        is_buy=not is_buy_entry,
        size=size,
        trigger_price=float(pl.stop),
        is_stop=True,
        reduce_only=True,
    )

    _log(f"[BROKER] submitted {pl.side} {pl.symbol} @ {entry_price} with {len(pl.tps)} TP(s) + 1 SL")
