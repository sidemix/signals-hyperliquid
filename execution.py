# execution.py
import logging
from dataclasses import dataclass
from typing import Optional

from broker.hyperliquid import submit_signal as hyper_submit

log = logging.getLogger("execution")

@dataclass
class ExecSignal:
    side: str                   # "LONG" | "SHORT"
    symbol: str                 # e.g. "BTC/USD"
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tif: Optional[str] = None
    notional_usd: Optional[float] = None
    timeframe: Optional[str] = None     # <-- added, optional

def execute_signal(sig: "ExecSignal"):
    """Fan-out to Hyperliquid broker."""
    log.info(
        "[EXEC] Dispatching to Hyperliquid: side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tf=%s",
        sig.side, sig.symbol, sig.entry_low, sig.entry_high,
        sig.stop_loss, sig.leverage, getattr(sig, "timeframe", None),
    )
    hyper_submit(sig)
    return "OK"
