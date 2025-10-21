# execution.py
from __future__ import annotations
import os
from dataclasses import dataclass, asdict
from typing import List, Tuple, Optional

# --------- Public dataclass used across the app ----------
@dataclass
class ExecSignal:
    symbol: str                      # "ETH/USD"
    side: str                        # "LONG" | "SHORT"
    entry_band: Tuple[float, float]  # (low, high)
    stop: float
    tps: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None

# --------- Simple allowlist helpers ----------
def _split_csv(env_name: str) -> list[str]:
    raw = os.getenv(env_name, "") or ""
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]

def is_author_allowed(author: str) -> bool:
    wl = _split_csv("AUTHOR_ALLOWLIST")
    if not wl:
        return True
    return author.strip().lower() in (x.lower() for x in wl)

def is_symbol_allowed(symbol: str) -> bool:
    # We expect allowlist like "BTC,ETH,SOL"
    wl = _split_csv("HYPER_ONLY_EXECUTE_SYMBOLS")
    if not wl:
        return True
    coin = symbol.split("/")[0].upper()
    return coin in {x.upper() for x in wl}

# --------- Main entry used by discord_listener ----------
def execute_signal(sig: ExecSignal) -> None:
    """
    Called by discord_listener after parsing a VIP message.
    Always forwards a dict payload to the broker to avoid signature mismatch.
    """
    payload = asdict(sig)

    # Logging (keep concise)
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
        ),
        flush=True,
    )

    # Import inside to avoid import cycles
    try:
        from broker import hyperliquid as broker  # type: ignore
    except Exception as e:
        raise RuntimeError(f"Could not import broker.hyperliquid: {e}")

    # Call the broker with a SINGLE dict arg (never bare **kwargs)
    try:
        broker.submit_signal(payload)  # <— important: pass one dict
    except Exception as e:
        raise RuntimeError(f"Broker submit failed: {e}")
