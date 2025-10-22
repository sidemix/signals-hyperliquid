# execution.py
from __future__ import annotations

import importlib
import logging
import os
import traceback
from collections import deque
from typing import Any, Optional

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# -----------------------------------------------------------------------------
# Dedupe for Discord message IDs (prevents double-runs on the same signal)
# -----------------------------------------------------------------------------
_SEEN_MSG_IDS = set()
_SEEN_ORDER = deque(maxlen=500)  # keep a rolling window of recent ids


def _get_msg_id(sig: Any) -> Optional[int]:
    """
    Attempts to extract a stable message id from the parsed signal.
    We try common attributes/keys: msg_id, id, message_id.
    """
    # Attribute style
    for name in ("msg_id", "message_id", "id"):
        if hasattr(sig, name):
            try:
                return getattr(sig, name)
            except Exception:
                pass
    # Dict style
    if isinstance(sig, dict):
        for name in ("msg_id", "message_id", "id"):
            if name in sig:
                try:
                    return sig[name]
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
    # try a few band variants (string rendering may already print it anyway)
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
        # Keep identical style to your logs
        print(f"[EXC] execution error: {e}")
        raise

    try:
        submit_fn(sig)
    except Exception as e:
        # Keep identical style to your logs
        print(f"[EXC] execution error: {e}")
        # For deep debugging, include traceback in server logs
        log.error("Submit traceback:\n%s", traceback.format_exc())
        raise


# -----------------------------------------------------------------------------
# If you run this module directly, you can test with a dummy signal:
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    class Dummy:
        # Simulate your parsed ExecSignal
        msg_id = 123
        side = "SHORT"
        symbol = "ETH/USD"
        # many parsers put a tuple here, our broker handles lots of variants anyway
        band = (3875.33, 3877.16)
        sl = 3899.68
        tpn = 6
        lev = 20
        tf = "5m"

        def __repr__(self):
            return "<Dummy SHORT ETH/USD band=(3875.33, 3877.16) SL=3899.68 TPn=6 lev=20 TF=5m>"

    try:
        execute_signal(Dummy())
    except Exception:
        # It's fine to error locally if broker isn't present; this block is just a smoke test.
        pass
