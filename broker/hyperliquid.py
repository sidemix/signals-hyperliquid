
# broker/hyperliquid.py
"""
Hyperliquid broker shim used by execution.execute_signal().

It defines a single public entry-point:
    submit_signal(exec_sig | **kwargs)

execution.py will call this with either an ExecSignal object
or with expanded keyword args. For now we:
  - honor DRY_RUN=true (no orders, just logs)
  - log a clear message showing the payload we would send
  - provide one place (_place_order_real) to integrate your
    actual Hyperliquid OTO bracket order code.

Once you wire _place_order_real, real orders will be placed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Tuple, Union, List

try:
    # For type hints only (no runtime dependency)
    from execution import ExecSignal  # noqa
except Exception:
    ExecSignal = Any  # type: ignore


def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _normalize_payload(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return a dict payload with keys:
      symbol, side, entry_band(tuple), stop, tps(list), leverage, timeframe
    """
    if isinstance(sig_or_kwargs, dict):
        symbol = sig_or_kwargs["symbol"]
        side = sig_or_kwargs["side"]
        band = sig_or_kwargs.get("entry_band") or (sig_or_kwargs["entry_low"], sig_or_kwargs["entry_high"])
        stop = float(sig_or_kwargs["stop"])
        tps  = list(sig_or_kwargs["tps"])
        lev  = sig_or_kwargs.get("leverage")
        tf   = sig_or_kwargs.get("timeframe")
    else:
        s = sig_or_kwargs  # ExecSignal
        symbol, side = s.symbol, s.side
        band = s.entry_band
        stop = float(s.stop)
        tps  = list(s.tps)
        lev  = s.leverage
        tf   = s.timeframe

    low, high = float(band[0]), float(band[1])
    return {
        "symbol": str(symbol).upper(),
        "side": side.upper(),
        "entry_band": (low, high),
        "stop": stop,
        "tps": tps,
        "leverage": lev,
        "timeframe": tf,
    }


def _dry_run() -> bool:
    return _env_bool("DRY_RUN", False)


def _log_preview(payload: Dict[str, Any]) -> None:
    sym = payload["symbol"]
    side = payload["side"]
    low, high = payload["entry_band"]
    print(
        f"[BROKER] {side} {sym} band=({low:.6f},{high:.6f}) "
        f"SL={payload['stop']:.6f} TPn={len(payload['tps'])} "
        f"lev={payload.get('leverage') or 'n/a'} TF={payload.get('timeframe') or 'n/a'}"
    )


# ---------- PLACEHOLDER you will wire to your real HL code ----------
def _place_order_real(payload: Dict[str, Any]) -> None:
    """
    Put your actual Hyperliquid placement code here.

    Typical steps:
      1) map "ETH/USD" -> coin "ETH", perp market
      2) compute size from TRADE_SIZE_USD / mark price
      3) post entry limit(s) in the provided band (or mid)
      4) post OTO: one stop loss + take-profit ladder

    If you already have code in this repo to place OTO orders,
    import and call it here with this normalized payload.
    """
    # Example hook if you already have something like:
    # from .place_oto import place_bracket
    # place_bracket(payload)
    raise NotImplementedError(
        "Wire your real Hyperliquid order placement here "
        "(sign + POST). For now, set DRY_RUN=true to preview."
    )


# ---------- Public entry used by execution.execute_signal ----------

def submit_signal(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]], **kw) -> None:
    """
    Main entry-point expected by execution.py.
    Accepts either an ExecSignal object or the expanded kwargs.
    """
    # Merge kwargs if caller used submit_signal(**kwargs)
    payload = _normalize_payload(sig_or_kwargs if kw == {} else {**sig_or_kwargs, **kw})  # type: ignore[arg-type]

    _log_preview(payload)

    if _dry_run():
        print("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    # Basic sanity check for required secrets (adapt names to your signer)
    api_key = os.getenv("HYPER_API_KEY", "")
    api_secret = os.getenv("HYPER_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("HYPER_API_KEY / HYPER_API_SECRET missing. Set them or enable DRY_RUN=true.")

    # Call your actual placement function
    _place_order_real(payload)
