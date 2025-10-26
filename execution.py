import logging
from dataclasses import dataclass
from typing import Optional

from broker.hyperliquid import submit_signal as hyper_submit

log = logging.getLogger("execution")

@dataclass
class ExecSignal:
    side: str
    symbol: str
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tif: Optional[str] = None
    notional_usd: Optional[float] = None
    timeframe: Optional[str] = None
    client_id: Optional[str] = None  # <-- important

def execute_signal(sig: "ExecSignal"):
    log.info(
        "[EXEC] Dispatching to Hyperliquid: side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tf=%s client_id=%s",
        sig.side, sig.symbol, sig.entry_low, sig.entry_high,
        sig.stop_loss, sig.leverage, getattr(sig, "timeframe", None),
        getattr(sig, "client_id", None),
    )
    hyper_submit(sig)
    return "OK"
