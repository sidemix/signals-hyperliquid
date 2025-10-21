# broker/hyperliquid.py
"""
Hyperliquid broker shim used by execution.execute_signal().

Public entry:
    submit_signal(exec_sig | **kwargs)

Behavior:
  - If DRY_RUN=true -> only logs what would be sent.
  - Otherwise, uses the official Hyperliquid Python SDK to place:
      * one entry limit inside the provided entry band (midpoint)
      * a reduce-only take-profit ladder (limits)
      * a stop-loss (trigger order)

Environment (required/optional):
  HYPER_EVM_PRIVKEY    - hex private key (0x....) used by the SDK signer
  HYPERLIQUID_BASE     - optional; defaults to https://api.hyperliquid.xyz
  TRADE_SIZE_USD       - optional; notional size per signal (default 100)
  DRY_RUN              - 'true' to preview only
"""

from __future__ import annotations

import os
import math
from typing import Any, Dict, List, Tuple, Union

# -------------------- Correct SDK imports --------------------
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from eth_account import Account
except Exception as e:
    Exchange = None  # type: ignore
    Info = None      # type: ignore
    Account = None   # type: ignore


# -------------------- Env helpers --------------------
def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")

def _dry_run() -> bool:
    return _env_bool("DRY_RUN", False)

def _base_url() -> str:
    return os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz")

def _trade_size_usd() -> float:
    try:
        return float(os.getenv("TRADE_SIZE_USD", "100"))
    except Exception:
        return 100.0


# -------------------- Clients --------------------
def _get_clients():
    """
    Returns (exchange, info) SDK clients, or raises if not available
    (unless DRY_RUN=true, in which case we return (None, None)).
    """
    if Exchange is None or Info is None or Account is None:
        if _dry_run():
            return None, None
        raise RuntimeError(
            "hyperliquid SDK and eth_account are required. "
            "pip install hyperliquid eth-account"
        )

    priv = os.getenv("HYPER_EVM_PRIVKEY", "").strip()
    if not priv:
        if _dry_run():
            return None, None
        raise RuntimeError("HYPER_EVM_PRIVKEY is not set.")

    wallet = Account.from_key(priv)
    base = _base_url()
    ex = Exchange(wallet, base)
    info = Info(base)
    return ex, info


# -------------------- Payload normalization --------------------
def _normalize_payload(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return dict:
      symbol, side, entry_band(tuple), stop, tps(list), leverage, timeframe
    """
    if isinstance(sig_or_kwargs, dict):
        symbol = sig_or_kwargs["symbol"]
        side = sig_or_kwargs["side"]
        band = sig_or_kwargs.get("entry_band") or (
            sig_or_kwargs.get("entry_low"),
            sig_or_kwargs.get("entry_high"),
        )
        stop = float(sig_or_kwargs["stop"])
        tps = list(sig_or_kwargs["tps"])
        lev = sig_or_kwargs.get("leverage")
        tf = sig_or_kwargs.get("timeframe")
    else:
        s = sig_or_kwargs  # ExecSignal
        symbol, side = s.symbol, s.side
        band = s.entry_band
        stop = float(s.stop)
        tps = list(s.tps)
        lev = s.leverage
        tf = s.timeframe

    low, high = float(band[0]), float(band[1])
    return {
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "entry_band": (low, high),
        "stop": stop,
        "tps": tps,
        "leverage": lev,
        "timeframe": tf,
    }


def _symbol_to_coin(symbol: str) -> str:
    # "ETH/USD" -> "ETH"
    return symbol.split("/")[0].upper().strip()


def _log_preview(payload: Dict[str, Any]) -> None:
    sym = payload["symbol"]
    side = payload["side"]
    low, high = payload["entry_band"]
    print(
        f"[BROKER] {side} {sym} band=({low:.6f},{high:.6f}) "
        f"SL={payload['stop']:.6f} TPn={len(payload['tps'])} "
        f"lev={payload.get('leverage') or 'n/a'} TF={payload.get('timeframe') or 'n/a'}"
    )


# -------------------- SDK helpers --------------------
def _get_mark_px(info: Any, coin: str) -> float:
    """
    Tries to get a fair price for size calc.
    Falls back to 0 (caller should guard).
    """
    try:
        # Info API surfaces top of book via l2 book or mark/oracle.
        # We prefer mark if available; fall back to mid of top of book.
        mark = info.mark_price(coin)
        if mark is not None:
            return float(mark)
    except Exception:
        pass

    try:
        book = info.l2_book(coin)
        # book = {"bids":[[px,sz],...], "asks":[[px,sz],...]}
        best_bid = float(book["bids"][0][0])
        best_ask = float(book["asks"][0][0])
        return (best_bid + best_ask) / 2.0
    except Exception:
        return 0.0


def _qty_from_usd(info: Any, coin: str, usd: float) -> float:
    px = _get_mark_px(info, coin)
    if px <= 0:
        return 0.0
    return max(usd / px, 0.0)


# -------------------- Real placement (SDK) --------------------
def _place_order_real(payload: Dict[str, Any]) -> None:
    """
    Places:
      - Entry limit at midpoint of entry band
      - RO TP ladder
      - Stop loss trigger
    Uses SDK Exchange.order() calls. Wrapped in try/except; prints any errors.
    """
    ex, info = _get_clients()
    if ex is None or info is None:
        # DRY_RUN true path
        print("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    symbol = payload["symbol"]
    coin = _symbol_to_coin(symbol)
    is_buy = payload["side"] == "LONG"
    (low, high) = payload["entry_band"]
    stop_px = float(payload["stop"])
    tps: List[float] = list(payload["tps"])

    # Size from TRADE_SIZE_USD
    usd = _trade_size_usd()
    size = _qty_from_usd(info, coin, usd)
    if size <= 0:
        raise RuntimeError("Could not compute size from mark price; aborting.")

    # Entry limit price
    limit_px = (low + high) / 2.0

    print(f"[BROKER] placing entry limit: coin={coin} is_buy={is_buy} sz={size:.6f} px={limit_px:.4f}")
    try:
        # SDK signature (as of HL Python SDK) supports "order" with limit / trigger dicts
        # Some versions use an "order_type" object with {"limit": {"tif": "Gtc"}}
        ex.order(
            coin=coin,
            is_buy=is_buy,
            sz=size,
            limit_px=limit_px,
            order_type={"limit": {"tif": "Gtc"}},
            reduce_only=False,
        )
    except Exception as e:
        raise RuntimeError(f"Entry order failed: {e}")

    # Take-profit ladder (reduce-only limits). Split size equally across n TP levels.
    if tps:
        n = len(tps)
        # guard tiny sizes
        leg = max(size / n, 1e-6)
        for tp_px in tps:
            print(f"[BROKER] placing TP limit: coin={coin} is_buy={not is_buy} sz={leg:.6f} px={tp_px:.4f} (reduce_only)")
            try:
                ex.order(
                    coin=coin,
                    is_buy=not is_buy,       # closing direction
                    sz=leg,
                    limit_px=float(tp_px),
                    order_type={"limit": {"tif": "Gtc"}},
                    reduce_only=True,
                )
            except Exception as e:
                # non-fatal: continue placing other TPs
                print(f"[WARN] TP order failed at {tp_px}: {e}")

    # Stop loss (trigger). On HL SDK, you pass a trigger dict with isMarket=true.
    # For a LONG, stop below -> sell; for a SHORT, stop above -> buy.
    print(f"[BROKER] placing STOP: coin={coin} trigger_px={stop_px:.4f} close_dir={not is_buy}")
    try:
        ex.order(
            coin=coin,
            is_buy=not is_buy,              # closing direction
            sz=size,
            limit_px=0.0,                   # ignored for market trigger
            order_type={"trigger": {"triggerPx": float(stop_px), "isMarket": True}},
            reduce_only=True,
        )
    except Exception as e:
        print(f"[WARN] Stop order failed: {e}")


# -------------------- Public entry --------------------
def submit_signal(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]], **kw) -> None:
    """
    Accepts either an ExecSignal object or expanded kwargs.
    """
    payload = _normalize_payload(sig_or_kwargs if not kw else {**sig_or_kwargs, **kw})  # type: ignore[arg-type]
    _log_preview(payload)

    if _dry_run():
        print("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    try:
        _place_order_real(payload)
    except Exception as e:
        raise RuntimeError(f"Broker submit failed: {e}")
