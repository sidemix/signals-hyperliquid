# execution.py
import importlib
import logging
import os
import traceback
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

log = logging.getLogger("execution")
logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)

@dataclass
class ExecSignal:
    # required fields
    side: str                  # "LONG" or "SHORT"
    symbol: str                # e.g. "BTC/USD"
    entry_low: float           # lower bound of entry band
    entry_high: float          # upper bound of entry band
    stop_loss: float           # absolute stop
    leverage: float = 0.0      # optional leverage (not required by HL SDK)
    tf: str = "5m"             # timeframe string (for logging only)
    tp_count: int = 0          # used only for logging

def _get_broker_submit() -> Callable[[ExecSignal], None]:
    """
    Lazily import the broker submit function so we can hot-swap brokers.
    """
    try:
        mod = importlib.import_module("broker.hyperliquid")
        return getattr(mod, "submit_signal")
    except Exception as e:
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}") from e

def execute_signal(sig: ExecSignal) -> None:
    """
    One entry point used by the Discord listener.
    """
    submit_fn = _get_broker_submit()

    band_str = f"({sig.entry_low:.6f}, {sig.entry_high:.6f})"
    log.info(
        "[EXEC] %s %s band=%s SL=%.6f lev=%.6f TF=%s",
        sig.side, sig.symbol, band_str, sig.stop_loss, sig.leverage, sig.tf,
    )

    try:
        submit_fn(sig)
    except Exception as e:
        log.error("[EXC] execution error: %s", e)
        raise
