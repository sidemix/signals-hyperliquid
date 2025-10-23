import importlib
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("execution")
log.setLevel(logging.INFO)


@dataclass
class ExecSignal:
    side: str               # "LONG" | "SHORT"
    symbol: str             # "BTC/USD", etc.
    entry_low: float        # lower band
    entry_high: float       # upper band
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tpn: Optional[int] = None
    timeframe: Optional[str] = None
    tif: Optional[str] = None  # e.g., "PostOnly" (optional)


def _get_broker_submit():
    """
    Lazy import so we can reload broker without restarting the bot.
    """
    try:
        mod = importlib.import_module("broker.hyperliquid")
        submit_fn = getattr(mod, "submit_signal")
        return submit_fn
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}") from e


def execute_signal(sig: ExecSignal) -> None:
    """
    Accept ExecSignal and forward to broker with logs.
    """
    log.info(
        "[EXEC] %s %s band=(%f, %f) SL=%s lev=%s TF=%s",
        sig.side, sig.symbol, sig.entry_low, sig.entry_high,
        str(sig.stop_loss), str(sig.leverage), str(sig.timeframe)
    )

    submit_fn = _get_broker_submit()
    try:
        submit_fn(sig)
    except Exception as e:  # noqa: BLE001
        log.error("[EXC] execution error: %s", e)
        raise
