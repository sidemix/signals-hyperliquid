# execution.py
"""
Public execution API used by discord_listener.py.

Exports:
  - ExecSignal            (dataclass the listener constructs)
  - is_symbol_allowed()   (env-based symbol allow-list)
  - execute_signal()      (hands parsed signal to broker.hyperliquid.submit_signal)

Design notes
------------
- We avoid any top-level import of the broker to prevent circular imports.
- The broker import happens *inside* execute_signal().
- The broker already accepts either ExecSignal or kwargs; we pass the object through.

Environment
-----------
HYPER_ONLY_EXECUTE_SYMBOLS   Comma-separated list like "BTC/USD,ETH/USD".  Empty -> allow all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple
import os
import logging


# ---------- Logging ----------
logger = logging.getLogger("execution")
if not logger.handlers:
    # Basic, quiet formatter – Render usually prefixes timestamps anyway
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[EXEC] %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


# ---------- Dataclass shared across the app ----------
@dataclass
class ExecSignal:
    symbol: str                          # e.g., "ETH/USD"
    side: str                            # "LONG" | "SHORT"
    entry_band: Tuple[float, float]      # (low, high)
    stop: float                          # absolute price
    tps: List[float]                     # take-profit ladder prices
    leverage: Optional[float] = None     # e.g., 20.0
    timeframe: Optional[str] = None      # e.g., "5m"


# ---------- Helpers ----------
def _normalize_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    # Minimal normalization: ensure slash, USD quote when obvious
    if "-" in s and "/USD" not in s and s.endswith("-USD"):
        base = s[:-4]
        return f"{base}/USD"
    return s


def is_symbol_allowed(symbol: str) -> bool:
    """
    Returns True if the symbol is allowed by HYPER_ONLY_EXECUTE_SYMBOLS.
    Empty/absent env means 'allow all'.
    """
    raw = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").strip()
    if not raw:
        return True
    allowed = {x.strip().upper() for x in raw.split(",") if x.strip()}
    return _normalize_symbol(symbol) in allowed


# ---------- Public entry called by the listener ----------
def execute_signal(sig: ExecSignal) -> None:
    """
    Main entry point used by discord_listener.py.

    Validates the symbol against the allow-list and forwards the signal
    to the broker's submit function. Any exceptions are allowed to bubble
    so the caller can log them cleanly.
    """
    # Normalize / sanity
    sig.symbol = _normalize_symbol(sig.symbol)

    if not is_symbol_allowed(sig.symbol):
        logger.info("%s not allowed by HYPER_ONLY_EXECUTE_SYMBOLS", sig.symbol)
        return

    logger.info(
        "%s %s band=(%.6f, %.6f) SL=%.6f TPn=%d lev=%s TF=%s",
        sig.side.upper(),
        sig.symbol,
        float(sig.entry_band[0]),
        float(sig.entry_band[1]),
        float(sig.stop),
        len(sig.tps),
        "n/a" if sig.leverage is None else f"{sig.leverage:.0f}",
        sig.timeframe or "n/a",
    )

    # Lazy import to avoid circulars (broker imports execution for type hints)
    try:
        from broker.hyperliquid import submit_signal
    except Exception as e:
        raise RuntimeError(f"Broker import failed: {e}")

    # Hand off to the broker; it accepts the ExecSignal object directly
    submit_signal(sig)


__all__ = ["ExecSignal", "is_symbol_allowed", "execute_signal"]
