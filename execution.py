# execution.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

# The broker module we call to place orders
from broker.hyperliquid import submit_signal as broker_submit


@dataclass
class ExecSignal:
    symbol: str
    side: str                     # "LONG" | "SHORT"
    entry_band: tuple[float, float]
    stop: float
    tps: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None


def is_symbol_allowed(symbol: str) -> bool:
    allow = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").strip()
    if not allow:
        return True
    # normalize like ETH/USD
    allowed = {s.strip().upper() for s in allow.split(",") if s.strip()}
    return symbol.upper() in allowed


def execute_signal(
    *,
    symbol: str,
    side: str,
    entry_band: tuple[float, float],
    stop: float,
    tps: List[float],
    leverage: Optional[float] = None,
    timeframe: Optional[str] = None,
) -> None:
    """
    Constructs ExecSignal and forwards to the broker.
    Honors DRY_RUN in the broker.
    """
    sig = ExecSignal(
        symbol=symbol,
        side=side.upper(),
        entry_band=(float(entry_band[0]), float(entry_band[1])),
        stop=float(stop),
        tps=[float(x) for x in tps],
        leverage=leverage,
        timeframe=timeframe,
    )

    # Small console log for visibility
    print(
        "[EXEC] {side} {sym} band=({lo:.6f}, {hi:.6f}) SL={sl:.6f} TPn={n} lev={lev} TF={tf}".format(
            side=sig.side,
            sym=sig.symbol,
            lo=sig.entry_band[0],
            hi=sig.entry_band[1],
            sl=sig.stop,
            n=len(sig.tps),
            lev=sig.leverage if sig.leverage is not None else "n/a",
            tf=sig.timeframe or "n/a",
        )
    )

    # Forward to broker
    broker_submit(sig)
