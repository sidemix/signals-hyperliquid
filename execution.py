# execution.py
import importlib
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

@dataclass
class ExecSignal:
    side: str                # "LONG" / "SHORT"
    symbol: str              # "ETH/USD"
    entry_low: float         # lower bound
    entry_high: float        # upper bound
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tps: Optional[list[float]] = None
    timeframe: Optional[str] = None
    uid: Optional[str] = None

def _get_broker_submit():
    try:
        mod = importlib.import_module("broker.hyperliquid")
        return getattr(mod, "submit_signal")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}") from e

def execute_signal(sig: ExecSignal) -> None:
    """
    Called by discord_listener when a message is parsed successfully.
    """
    # remember: broker handles allowed symbols and order placement
    if not isinstance(sig, ExecSignal):
        raise TypeError("execute_signal expects ExecSignal")
    side = sig.side.upper()
    log.info(
        f"[EXEC] {side} {sig.symbol} band=({sig.entry_low}, {sig.entry_high}) "
        f"{'SL=' + str(sig.stop_loss) if sig.stop_loss else ''} "
        f"{'lev=' + str(sig.leverage) if sig.leverage else ''} "
        f"{'TF=' + sig.timeframe if sig.timeframe else ''}"
    )
    submit_fn = _get_broker_submit()
    try:
        submit_fn(sig)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"[EXC] execution error: {e}\n{tb}")
        raise
