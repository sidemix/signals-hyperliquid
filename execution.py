# execution.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# broker entry — must exist
from broker.hyperliquid import submit_signal as broker_submit


def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _read_allowlist() -> set:
    """SYMBOL allowlist: comma separated e.g. 'BTC/USD, ETH/USD' (case insensitive)."""
    raw = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "")
    if not raw.strip():
        return set()
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def is_symbol_allowed(symbol: str) -> bool:
    """If env is empty -> allow all. Accepts ETH/USD and ETH-USD forms."""
    allowed = _read_allowlist()
    if not allowed:
        return True
    s = symbol.strip().upper()
    return (s in allowed) or (s.replace("-", "/") in allowed) or (s.replace("/", "-") in allowed)


@dataclass
class ExecSignal:
    symbol: str
    side: str                     # LONG | SHORT
    entry_band: Tuple[float, float]
    stop: float
    tps: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None


def execute_signal(sig: ExecSignal) -> None:
    """
    Main entry the listener calls. Forwards to broker after checks.
    """
    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not allowed by HYPER_ONLY_EXECUTE_SYMBOLS")
        return

    side = sig.side.upper()
    if side not in ("LONG", "SHORT"):
        print(f"[SKIP] Unknown side: {sig.side}")
        return

    low, high = sig.entry_band
    low, high = float(min(low, high)), float(max(low, high))

    if not sig.tps or not isinstance(sig.tps, Sequence):
        print("[SKIP] No take-profit levels present.")
        return

    print(
        f"[EXEC] {side} {sig.symbol} band=({low:.6f}, {high:.6f}) "
        f"SL={sig.stop:.6f} TPn={len(sig.tps)} "
        f"lev={sig.leverage if sig.leverage is not None else 'n/a'} "
        f"TF={sig.timeframe or 'n/a'}"
    )

    try:
        broker_submit(
            {
                "symbol": sig.symbol,
                "side": side,
                "entry_band": (low, high),
                "stop": float(sig.stop),
                "tps": [float(x) for x in sig.tps],
                "leverage": sig.leverage,
                "timeframe": sig.timeframe,
            }
        )
        print(f"[EXEC] submitted {side} {sig.symbol} ({low}, {high}) SL={sig.stop}")
    except Exception as e:
        print(f"[EXC] execution/broker error: {e}")
