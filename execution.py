# execution.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# The broker file must expose `submit_signal(payload_or_execsig)`
# (you already have broker/hyperliquid.py)
from broker.hyperliquid import submit_signal as broker_submit


# --------- helpers ---------

def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _read_allowlist() -> set:
    raw = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "")
    if not raw.strip():
        return set()
    # split by comma, strip spaces, keep uppercase
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def is_symbol_allowed(symbol: str) -> bool:
    """
    If HYPER_ONLY_EXECUTE_SYMBOLS is empty -> allow all.
    Otherwise symbol must be present (case-insensitive).
    Accepts both 'ETH/USD' and 'ETH-USD' forms.
    """
    allowed = _read_allowlist()
    if not allowed:
        return True
    s1 = symbol.strip().upper()
    s2 = s1.replace("-", "/")
    s3 = s1.replace("/", "-")
    return (s1 in allowed) or (s2 in allowed) or (s3 in allowed)


# --------- data model ---------

@dataclass
class ExecSignal:
    symbol: str                          # e.g. "ETH/USD"
    side: str                            # "LONG" | "SHORT"
    entry_band: Tuple[float, float]      # (low, high)
    stop: float
    tps: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None


# --------- main entry ---------

def execute_signal(sig: ExecSignal) -> None:
    """
    Normalizes and forwards a parsed signal to the broker.
    `sig` must be an ExecSignal (provided by discord_listener).
    """
    # symbol gate
    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not allowed by HYPER_ONLY_EXECUTE_SYMBOLS")
        return

    side = sig.side.upper()
    if side not in ("LONG", "SHORT"):
        print(f"[SKIP] Unknown side: {sig.side}")
        return

    low, high = float(sig.entry_band[0]), float(sig.entry_band[1])
    if low > high:
        low, high = high, low

    # quick sanity checks
    if not sig.tps or not isinstance(sig.tps, Sequence):
        print("[SKIP] No take-profit levels present.")
        return

    # Log preview
    print(
        f"[EXEC] {side} {sig.symbol} band=({low:.6f}, {high:.6f}) "
        f"SL={sig.stop:.6f} TPn={len(sig.tps)} "
        f"lev={sig.leverage if sig.leverage is not None else 'n/a'} TF={sig.timeframe or 'n/a'}"
    )

    # DRY_RUN behavior is handled inside the broker too (it prints and returns).
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
