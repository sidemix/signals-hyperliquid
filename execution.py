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

    We include multiple alias fields to match your parser’s output without
    forcing refactors elsewhere (e.g., band vs. band_low/band_high, sl vs stop_loss).
    """
    # Discord/message metadata
    msg_id: Optional[int] = None
    message_id: Optional[int] = None
    id: Optional[int] = None

    # Core signal
    side: str = ""           # "LONG" | "SHORT"
    symbol: str = ""         # "ETH/USD", "BTC/USD", etc.

    # Entry band (any of these may be present)
    band: Optional[Tuple[float, float]] = None
    band_low: Optional[float] = None
    band_high: Optional[float] = None

    # Risk & targets
    stop_loss: Optional[float] = None
    sl: Optional[float] = None
    tp_count: Optional[int] = None
    tpn: Optional[int] = None

    # Leverage & timeframe (aliases supported)
    leverage: Optional[int] = None
    lev: Optional[int] = None
    timeframe: str = ""
    tf: Optional[str] = None

    # For debugging / passthrough
    raw: Any = None

    def __repr__(self) -> str:
        # Nice, compact summary that matches your log style
        # Prefer band tuple if present; else synthesize from lows/highs
        band_txt = None
        if self.band is not None:
            try:
                band_txt = f"({float(self.band[0]):.2f}, {float(self.band[1]):.2f})"
            except Exception:
                band_txt = str(self.band)
        elif self.band_low is not None and self.band_high is not None:
            band_txt = f"({float(self.band_low):.2f}, {float(self.band_high):.2f})"

        sl_val = self.stop_loss if self.stop_loss is not None else self.sl
        tpn_val = self.tp_count if self.tp_count is not None else self.tpn
        lev_val = self.leverage if self.leverage is not None else self.lev
        tf_val = self.timeframe or (self.tf or "")

        parts = []
        if self.side:
            parts.append(self.side.upper())
        if self.symbol:
            parts.append(self.symbol)
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
                if isinstance(val, (int,)) or (isinstance(val, str) and val.isdigit()):
                    return int(val)
                return val
            except Exception:
                pass
    # Dict style
    if isinstance(sig, dict):
        for name in ("msg_id", "message_id", "id"):
            if name in sig:
                try:
                    val = sig[name]
                    if isinstance(val, (int,)) or (isinstance(val, str) and val.isdigit()):
                        return int(val)
                    return val
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
    band = read("band") or read("entry_band") or read("entry") or read("range") or ""
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
    # Minimal manual test
    s = ExecSignal(
        msg_id=123,
        side="SHORT",
        symbol="ETH/USD",
        band=(3875.33, 3877.16),
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
