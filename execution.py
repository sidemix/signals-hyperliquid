import sys
import logging
from dataclasses import dataclass
from typing import Optional

# Root logger to stdout
root = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(h)
root.setLevel(logging.INFO)

log = logging.getLogger("execution")

# Import our local submitter module (renamed to avoid conflict with pip package)
from hyper_submit import submit_signal as hl_submit

# Dataclass used by parser (so `from execution import ExecSignal` works)
@dataclass
class ExecSignal:
    side: str
    symbol: str
    entry_low: float
    entry_high: float
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tif: Optional[str] = None
    client_id: Optional[str] = None
    notional_usd: Optional[float] = None

def execute_signal(sig) -> None:
    """Unified executor that always calls our HyperLiquid submitter and surfaces logs."""
    try:
        log.info(
            "[EXEC] Dispatching to Hyperliquid: side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tif=%s client_id=%s",
            getattr(sig, "side", None),
            getattr(sig, "symbol", None),
            getattr(sig, "entry_low", None),
            getattr(sig, "entry_high", None),
            getattr(sig, "stop_loss", None),
            getattr(sig, "leverage", None),
            getattr(sig, "tif", None),
            getattr(sig, "client_id", None),
        )
        hl_submit(sig)
    except Exception as e:
        log.exception("[EXEC] ERROR in execute_signal: %s", e)
        raise
