# execution.py
"""
Execution layer for Hyperliquid auto-trader.

- Defines ExecSignal (uses entry_band=(low, high)).
- Provides make_exec_signal(...) for backward-compat construction.
- Exposes async execute_signal(exec_sig) which delegates to the broker layer.
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple, Literal, Callable, Any, Awaitable
import inspect


# ---------- Data model ----------

@dataclass
class ExecSignal:
    symbol: str                           # e.g., "ETH/USD"
    side: Literal["LONG", "SHORT"]
    entry_band: Tuple[float, float]       # (low, high)
    stop: float
    tps: List[float]
    leverage: Optional[float] = None      # e.g., 20
    timeframe: Optional[str] = None       # e.g., "5m"


def make_exec_signal(**kwargs) -> ExecSignal:
    """
    Backward-compatible constructor.

    Accepts either:
      - entry_band=(low, high)    OR
      - entry_low=..., entry_high=...

    Any extra supported ExecSignal fields are passed through.
    """
    if "entry_band" in kwargs:
        band = kwargs.pop("entry_band")
    else:
        # Support old callers that passed entry_low/entry_high
        low = float(kwargs.pop("entry_low"))
        high = float(kwargs.pop("entry_high"))
        band = (low, high)

    return ExecSignal(entry_band=band, **kwargs)


# ---------- Broker delegation ----------

# We try to import a single entry-point from your broker layer.
# Name it however you like; these are common choices.
_BROKER_FUNCS: List[str] = [
    "submit_signal",     # prefer this
    "execute_signal",    # or this
    "place_signal",      # or this
]

_broker_submit: Optional[Callable[..., Any]] = None
try:
    from broker import hyperliquid as _hl  # your repo typically has broker/hyperliquid.py

    for fname in _BROKER_FUNCS:
        if hasattr(_hl, fname):
            _broker_submit = getattr(_hl, fname)
            break
except Exception:
    _hl = None
    _broker_submit = None


def _dry_run() -> bool:
    return str(os.getenv("DRY_RUN", "false")).strip().lower() in ("1", "true", "yes", "on")


async def _maybe_await(func: Callable, *args, **kwargs):
    """Call func; await it if it's a coroutine function or returns a coroutine."""
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


# ---------- Public API ----------

async def execute_signal(sig: ExecSignal) -> None:
    """
    Execute the signal by delegating to the broker layer.
    If DRY_RUN=true, only logs.

    Broker function should accept one of:
      - the ExecSignal object itself
      - or the expanded fields (keyword args)
    """
    low, high = sig.entry_band
    print(
        f"[EXEC] {sig.side} {sig.symbol} band=({low:.6f}, {high:.6f}) "
        f"SL={sig.stop:.6f} TPn={len(sig.tps)} lev={sig.leverage or 'n/a'} TF={sig.timeframe or 'n/a'}"
    )

    if _dry_run():
        print("[EXEC] DRY_RUN=true — not sending to exchange.")
        return

    if _broker_submit is None:
        raise RuntimeError(
            "No broker submission function found. "
            "Implement one in broker/hyperliquid.py and export it as "
            f"one of: {', '.join(_BROKER_FUNCS)}"
        )

    # Be generous with the broker signature: try (object) first, then kwargs.
    try:
        await _maybe_await(_broker_submit, sig)
        return
    except TypeError:
        # Fall back to kwargs
        await _maybe_await(
            _broker_submit,
            symbol=sig.symbol,
            side=sig.side,
            entry_band=sig.entry_band,
            stop=sig.stop,
            tps=sig.tps,
            leverage=sig.leverage,
            timeframe=sig.timeframe,
        )
