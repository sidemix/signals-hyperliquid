# execution.py
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("execution")


@dataclass
class ExecSignal:
    # Required
    side: str               # "LONG" or "SHORT"
    symbol: str             # e.g. "BTC/USD"
    entry_low: float        # lower bound of entry band
    entry_high: float       # upper bound of entry band

    # Optional
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    timeframe: Optional[str] = None
    take_profits: List[float] = field(default_factory=list)

    # Backward-compat shim for older broker code that expects entry_band
    @property
    def entry_band(self) -> Tuple[float, float]:
        return (self.entry_low, self.entry_high)


def _get_broker_submit():
    """
    Dynamically import broker.hyperliquid and return its submit_signal() fn.
    """
    try:
        mod = importlib.import_module("broker.hyperliquid")
        if not hasattr(mod, "submit_signal"):
            raise RuntimeError("broker.hyperliquid missing submit_signal()")
        return getattr(mod, "submit_signal")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}") from e


def _fmt(n: Optional[float]) -> str:
    return f"{n:.6f}" if isinstance(n, (int, float)) else "None"


def execute_signal(sig: ExecSignal) -> None:
    """
    Route a parsed ExecSignal to the active broker.
    """
    # Human-friendly console line that matches your earlier logs
    log.info(
        "[EXEC] %s %s band=(%s, %s) SL=%s lev=%s TF=%s",
        (sig.side or "").upper(),
        sig.symbol,
        _fmt(sig.entry_low),
        _fmt(sig.entry_high),
        _fmt(sig.stop_loss),
        _fmt(sig.leverage),
        sig.timeframe or "None",
    )

    submit_fn = _get_broker_submit()
    try:
        submit_fn(sig)
    except Exception as e:
        log.exception("[EXC] execution error: %s", e)
        raise

