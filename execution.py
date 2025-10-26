import sys
import os
import logging
from typing import Optional, Any

# Root logger to stdout
root = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(h)
root.setLevel(logging.INFO)

log = logging.getLogger("execution")

# Make sure the app root is importable (Render runs from /app)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Robust import of our local submitter (avoid collision with pip 'hyperliquid' package)
try:
    from hyper_submit import submit_signal as hl_submit
except ModuleNotFoundError:
    # Try package-style relative import if files live in a package folder
    try:
        from .hyper_submit import submit_signal as hl_submit  # type: ignore
    except Exception as e:
        raise

class ExecSignal:
    """
    Flexible container for trading signals.
    Accepts any extra fields the parser may include (e.g. timeframe, take_profit).
    Known fields have defaults to keep type hints clear.
    """
    # known/common fields with defaults
    side: str
    symbol: str
    entry_low: float
    entry_high: float
    stop_loss: Optional[float]
    leverage: Optional[float]
    tif: Optional[str]
    client_id: Optional[str]
    notional_usd: Optional[float]
    timeframe: Optional[str]

    def __init__(self, **kwargs: Any):
        # defaults
        self.side = kwargs.pop("side", "")
        self.symbol = kwargs.pop("symbol", "")
        self.entry_low = kwargs.pop("entry_low", None)
        self.entry_high = kwargs.pop("entry_high", None)
        self.stop_loss = kwargs.pop("stop_loss", None)
        self.leverage = kwargs.pop("leverage", None)
        self.tif = kwargs.pop("tif", None)
        self.client_id = kwargs.pop("client_id", None)
        self.notional_usd = kwargs.pop("notional_usd", None)
        self.timeframe = kwargs.pop("timeframe", None)

        # absorb any additional fields from the parser without breaking
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        return (
            f"ExecSignal(side={self.side!r}, symbol={self.symbol!r}, "
            f"entry_low={self.entry_low!r}, entry_high={self.entry_high!r}, "
            f"stop_loss={self.stop_loss!r}, leverage={self.leverage!r}, "
            f"tif={self.tif!r}, client_id={self.client_id!r}, "
            f"notional_usd={self.notional_usd!r}, timeframe={self.timeframe!r})"
        )

def execute_signal(sig) -> None:
    """Unified executor that always calls our HyperLiquid submitter and surfaces logs."""
    try:
        log.info(
            "[EXEC] Dispatching to Hyperliquid: side=%s symbol=%s band=(%s,%s) sl=%s lev=%s tif=%s client_id=%s timeframe=%s",
            getattr(sig, "side", None),
            getattr(sig, "symbol", None),
            getattr(sig, "entry_low", None),
            getattr(sig, "entry_high", None),
            getattr(sig, "stop_loss", None),
            getattr(sig, "leverage", None),
            getattr(sig, "tif", None),
            getattr(sig, "client_id", None),
            getattr(sig, "timeframe", None),
        )
        hl_submit(sig)
    except Exception as e:
        log.exception("[EXEC] ERROR in execute_signal: %s", e)
        raise
