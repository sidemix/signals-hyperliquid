"""
Minimal Hyperliquid broker shim.

execution.py imports:
    from broker.hyperliquid import submit_signal

We expose exactly that symbol. In DRY_RUN=true, we only log the order
we would place. When DRY_RUN=false, we check for required secrets and
call a placeholder you can wire to real HL placement.

This module is intentionally import-safe: no SDK import at top-level.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Tuple, Union, List

# Optional type hint (no runtime dependency on your app's objects)
try:
    from execution import ExecSignal  # only for typing
except Exception:  # pragma: no cover
    ExecSignal = Any  # type: ignore


# -------- helpers --------

def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _normalize(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]]) -> Dict[str, Any]:
    """
    Return a dict with keys:
      symbol, side, entry_band(tuple[low,high]), stop, tps(list[float]),
      leverage(optional), timeframe(optional)
    """
    if isinstance(sig_or_kwargs, dict):
        d = dict(sig_or_kwargs)
        # support either entry_band or entry_low/entry_high
        if "entry_band" in d and d["entry_band"]:
            low, high = d["entry_band"]
        else:
            low, high = d.get("entry_low"), d.get("entry_high")
        payload = {
            "symbol": str(d["symbol"]).upper(),
            "side": str(d["side"]).upper(),
            "entry_band": (float(low), float(high)),
            "stop": float(d["stop"]),
            "tps": [float(x) for x in d.get("tps", [])],
            "leverage": d.get("leverage"),
            "timeframe": d.get("timeframe"),
        }
        return payload

    # ExecSignal object path
    s = sig_or_kwargs
    low, high = s.entry_band
    return {
        "symbol": str(s.symbol).upper(),
        "side": str(s.side).upper(),
        "entry_band": (float(low), float(high)),
        "stop": float(s.stop),
        "tps": [float(x) for x in s.tps],
        "leverage": getattr(s, "leverage", None),
        "timeframe": getattr(s, "timeframe", None),
    }


def _log_preview(p: Dict[str, Any]) -> None:
    low, high = p["entry_band"]
    print(
        "[BROKER] "
        f"{p['side']} {p['symbol']} "
        f"band=({low:.6f},{high:.6f}) SL={p['stop']:.6f} "
        f"TPn={len(p['tps'])} lev={p.get('leverage') or 'n/a'} TF={p.get('timeframe') or 'n/a'}"
    )


def _place_order_real(payload: Dict[str, Any]) -> None:
    """
    TODO: Wire your real Hyperliquid order placement here (sign + POST).
    For now we just raise so it's obvious.

    If you’re using the official SDK later, import it *inside* this
    function to avoid import errors at module import time.
    """
    raise NotImplementedError(
        "Replace _place_order_real with real HL placement. Until then, run with DRY_RUN=true."
    )


# -------- public entry-point expected by execution.py --------

def submit_signal(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]], **kw) -> None:
    """
    Main entry for the broker adapter. execution.py calls this.
    Accepts either ExecSignal object or expanded kwargs.
    """
    # merge kwargs if caller used submit_signal(**kwargs)
    data = sig_or_kwargs if not kw else {**sig_or_kwargs, **kw}  # type: ignore[arg-type]
    payload = _normalize(data)

    _log_preview(payload)

    if _env_bool("DRY_RUN", False):
        print("[BROKER] DRY_RUN=true — not sending to exchange.")
        return

    # Only check secrets in live mode
    evm_priv = os.getenv("HYPER_EVM_PRIVKEY", "").strip()
    if not evm_priv:
        raise RuntimeError("HYPER_EVM_PRIVKEY is not set.")

    # call your real placement
    _place_order_real(payload)


__all__ = ["submit_signal"]
