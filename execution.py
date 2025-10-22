"""
execution.py
Handles parsed trading signals and routes them to the broker adapter.
Now includes runtime-safe broker loader for hyperliquid.
"""

from __future__ import annotations
import os
import traceback
import importlib
from dataclasses import dataclass
from typing import Any, List, Tuple, Optional, Union


# ============================================================
# Dataclass for structured trade signal
# ============================================================

@dataclass
class ExecSignal:
    symbol: str
    side: str
    entry_band: Tuple[float, float]
    stop: float
    tps: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None


# ============================================================
# Helper utilities
# ============================================================

def _env_bool(key: str, default: bool = False) -> bool:
    """Read boolean env var."""
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _in_allow_list(symbol: str) -> bool:
    """
    Check if a symbol is allowed to execute based on HYPER_ONLY_EXECUTE_SYMBOLS.
    e.g., HYPER_ONLY_EXECUTE_SYMBOLS=BTC/USD,ETH/USD,SOL/USD
    """
    allowed = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "")
    if not allowed:
        return True
    syms = [s.strip().upper() for s in allowed.split(",") if s.strip()]
    return symbol.upper() in syms


# ============================================================
# Dynamic Broker Import (robust version)
# ============================================================

def _get_broker_submit():
    """
    Dynamically import the broker.hyperliquid.submit_signal callable.
    Gives detailed error logs if import fails or symbol missing.
    """
    try:
        mod = importlib.import_module("broker.hyperliquid")
    except Exception:
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}")

    submit_fn = getattr(mod, "submit_signal", None)
    if not callable(submit_fn):
        attrs = ", ".join(a for a in dir(mod) if not a.startswith("_"))
        raise RuntimeError(
            f"broker.hyperliquid has no callable 'submit_signal'. Found attributes: {attrs}"
        )

    return submit_fn


# ============================================================
# Core execution logic
# ============================================================

def execute_signal(sig: ExecSignal) -> None:
    """
    Executes a parsed signal by routing to the broker adapter.
    """
    try:
        # Check allowed list
        if not _in_allow_list(sig.symbol):
            print(f"[EXEC] {sig.symbol} not allowed by HYPER_ONLY_EXECUTE_SYMBOLS")
            return

        # Log high-level signal info
        print(
            f"[EXEC] {sig.side.upper()} {sig.symbol.upper()} "
            f"band=({sig.entry_band[0]:.6f}, {sig.entry_band[1]:.6f}) "
            f"SL={sig.stop:.6f} TPn={len(sig.tps)} lev={sig.leverage or 'n/a'} TF={sig.timeframe or 'n/a'}"
        )

        # Dynamically load broker submit function
        submit_fn = _get_broker_submit()

        # Call broker with structured signal
        submit_fn(sig)

        print(
            f"[EXEC] submitted {sig.side.upper()} {sig.symbol.upper()} "
            f"({sig.entry_band[0]:.2f}, {sig.entry_band[1]:.2f}) SL={sig.stop:.2f}"
        )

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[EXC] execution error: {e}\n{tb}")


# ============================================================
# Optional: Debug helper (for local testing)
# ============================================================

if __name__ == "__main__":
    print("Testing execution.py with dummy signal...")

    test_signal = ExecSignal(
        symbol="ETH/USD",
        side="SHORT",
        entry_band=(3875.33, 3877.16),
        stop=3899.68,
        tps=[3863.16, 3853.43, 3843.69],
        leverage=20,
        timeframe="5m",
    )

    execute_signal(test_signal)
