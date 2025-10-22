# execution.py
from __future__ import annotations

import importlib
import logging
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional, Tuple

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# -----------------------------------------------------------------------------
# ExecSignal (expected by discord_listener)
# -----------------------------------------------------------------------------
@dataclass
class ExecSignal:
    """
    Flexible container for parsed trade signals.

    Includes many alias fields so upstream parsers can pass different names
    (e.g., entry_band, range, price_band, etc.) without breaking imports.
    """
    # Discord/message metadata
    msg_id: Optional[int] = None
    message_id: Optional[int] = None
    id: Optional[int] = None

    # Core signal
    side: str = ""            # "LONG" | "SHORT"
    symbol: str = ""          # "ETH/USD", "BTC/USD", etc.
    direction: Optional[str] = None
    ticker: Optional[str] = None
    pair: Optional[str] = None

    # Entry band variants
    band: Optional[Tuple[float, float]] = None
    entry_band: Optional[Tuple[float, float]] = None
    entry: Optional[Tuple[float, float]] = None
    range: Optional[Tuple[float, float]] = None
    price_band: Optional[Tuple[float, float]] = None
    band_bounds: Optional[Tuple[float, float]] = None

    # Separate low/high aliases (some parsers pass scalar fields)
    band_low: Optional[float] = None
    band_high: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    lower_band: Optional[float] = None
    upper_band: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None

    # Risk & targets (aliases)
    stop_loss: Optional[float] = None
    sl: Optional[float] = None
    stop: Optional[float] = None
    stopPrice: Optional[float] = None

    tp_count: Optional[int] = None
    tpn: Optional[int] = None
    tpN: Optional[int] = None
    take_profit_count: Optional[int] = None

    # Leverage & timeframe (aliases)
    leverage: Optional[int] = None
    lev: Optional[int] = None
    x: Optional[int] = None

    timeframe: str = ""
    tf: Optional[str] = None

    # For debugging / passthrough
    raw: Any = None

    def __repr__(self) -> str:
        # Prefer the most canonical band visible
        band_tuple = (
            self.band or self.entry_band or self.entry or self.range
            or self.price_band or self.band_bounds
        )

        # If only scalar lows/highs exist, synthesize a band
        if band_tuple is None:
            low = (self.band_low or self.entry_low or self.range_low
                   or self.lower_band or self.min_price or self.low or self.lo or self.min)
            high = (self.band_high or self.entry_high or self.range_high
                    or self.upper_band or self.max_price or self.high or self.hi or self.max)
            if low is not None and high is not None:
                band_tuple = (float(low), float(high))

        band_txt = None
        if band_tuple is not None:
            try:
                band_txt = f"({float(band_tuple[0]):.2f}, {float(band_tuple[1]):.2f})"
            except Exception:
                band_txt = str(band_tuple)

        sl_val = (
            self.stop_loss if self.stop_loss is not None else
            (self.sl if self.sl is not None else (self.stop if self.stop is not None else self.stopPrice))
        )
        tpn_val = self.tp_count if self.tp_count is not None else (self.tpn if self.tpn is not None else self.tpN if self.tpN is not None else self.take_profit_count)
        lev_val = self.leverage if self.leverage is not None else (self.lev if self.lev is not None else self.x)
        tf_val = self.timeframe or (self.tf or "")

        # Canonical side/symbol
        side = (self.side or self.direction or "")
        symbol = self.symbol or self.ticker or self.pair or ""

        parts = []
        if side:
            parts.append(side.upper())
        if symbol:
            parts.append(symbol)
        head = " ".join(parts) if parts else "signal"

        extras = []
        if band_txt:
            extras.append(f"band={band_txt}")
        if sl_val is not None:
            extras.append(f"SL={sl_val}")
        if tpn_val is not None:
            extras.append(f"TPn={tpn_val}")
        if lev_val is not None:
            extras.append(f"lev={lev_val}")
        if tf_val:
            extras.append(f"TF={tf_val}")

        body = " ".join(extras)
        return f"<ExecSignal {head} {body}>".strip()


# -----------------------------------------------------------------------------
# Dedupe for Discord message IDs (prevents double-runs on the same signal)
# -----------------------------------------------------------------------------
_SEEN_MSG_IDS = set()
_SEEN_ORDER = deque(maxlen=500)  # keep a rolling window of recent ids


def _get_msg_id(sig: Any) -> Optional[int]:
    """
    Attempts to extract a stable message id from the parsed signal.
    We try common attributes/keys: msg_id, message_id, id.
    """
    # Attribute style
    for name in ("msg_id", "message_id", "id"):
        if hasattr(sig, name):
            try:
                val = getattr(sig, name)
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and val.isdigit():
                    return int(val)
            except Exception:
                pass
    # Dict style
    if isinstance(sig, dict):
        for name in ("msg_id", "message_id", "id"):
            if name in sig:
                try:
                    val = sig[name]
                    if isinstance(val, int):
                        return val
                    if isinstance(val, str) and val.isdigit():
                        return int(val)
                except Exception:
                    pass
    return None


def _already_processed(msg_id: Optional[int]) -> bool:
    """
    Returns True if we've handled this message id recently.
    """
    if msg_id is None:
        return False
    if msg_id in _SEEN_MSG_IDS:
        print(f"[SKIP] duplicate message id={msg_id}")
        return True
    _SEEN_MSG_IDS.add(msg_id)
    _SEEN_ORDER.append(msg_id)
    # Keep sets bounded
    if len(_SEEN_MSG_IDS) > _SEEN_ORDER.maxlen:
        old = _SEEN_ORDER.popleft()
        _SEEN_MSG_IDS.discard(old)
    return False


# -----------------------------------------------------------------------------
# Broker loader
# -----------------------------------------------------------------------------
def _get_broker_submit():
    """
    Dynamically import the Hyperliquid broker and return its submit_signal().
    Shows a friendly traceback if import fails.
    """
    try:
        mod = importlib.import_module("broker.hyperliquid")
        if not hasattr(mod, "submit_signal"):
            raise AttributeError("broker.hyperliquid has no submit_signal()")
        return mod.submit_signal
    except Exception:
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}")


# -----------------------------------------------------------------------------
# Pretty summaries for logs (optional)
# -----------------------------------------------------------------------------
def _summarize(sig: Any) -> str:
    """
    Best-effort summary line for logs. Does not rely on exact field names.
    """
    def read(*names, default=None):
        for n in names:
            if hasattr(sig, n):
                return getattr(sig, n)
            if isinstance(sig, dict) and n in sig:
                return sig[n]
        return default

    side = (read("side", "direction", default="")).upper()
    symbol = read("symbol", "ticker", "pair", default="")
    band = (
        read("band") or read("entry_band") or read("entry") or
        read("range") or read("price_band") or read("band_bounds") or ""
    )
    sl = read("stop_loss", "sl", "stop", "stopPrice", default=None)
    tpn = read("tp_count", "tpn", "tpN", "take_profit_count", default=None)
    lev = read("leverage", "lev", "x", default=None)
    tf = read("timeframe", "tf", default="")

    core = []
    if side:
        core.append(side)
    if symbol:
        core.append(symbol)
    head = " ".join(core) if core else "signal"

    parts = []
    if band:
        parts.append(f"band={band}")
    if sl is not None:
        parts.append(f"SL={sl}")
    if tpn is not None:
        parts.append(f"TPn={tpn}")
    if lev is not None:
        parts.append(f"lev={lev}")
    if tf:
        parts.append(f"TF={tf}")
    detail = " ".join(parts)
    return f"{head} {detail}".strip()


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def execute_signal(sig: Any) -> None:
    """
    Top-level coordinator called by your Discord handler after a signal is parsed.
    - Dedupes by Discord message id
    - Loads broker once per run and submits
    - Surfaces clear error messages while preserving traceback for debugging
    """
    msg_id = _get_msg_id(sig)
    if _already_processed(msg_id):
        return

    summary = _summarize(sig)
    if summary:
        print(f"[EXEC] {summary}")

    try:
        submit_fn = _get_broker_submit()
    except Exception as e:
        print(f"[EXC] execution error: {e}")
        raise

    try:
        submit_fn(sig)
    except Exception as e:
        print(f"[EXC] execution error: {e}")
        log.error("Submit traceback:\n%s", traceback.format_exc())
        raise


# -----------------------------------------------------------------------------
# Local smoke test
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Minimal manual test with entry_band alias
    s = ExecSignal(
        msg_id=123,
        side="SHORT",
        symbol="ETH/USD",
        entry_band=(3875.33, 3877.16),
        sl=3899.68,
        tpn=6,
        lev=20,
        tf="5m",
    )
    try:
        execute_signal(s)
    except Exception:
        # It's fine to error locally if broker isn't present; this is only a smoke test.
        pass
