# execution.py
from __future__ import annotations

import importlib
import logging
import traceback
from collections import deque
from typing import Any, Optional, Tuple, List

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# -----------------------------------------------------------------------------
# ExecSignal: robust, accepts ANY kwargs and normalizes common aliases
# -----------------------------------------------------------------------------
class ExecSignal:
    """
    Flexible container for parsed trade signals.

    - Accepts ANY kwargs from upstream parser (no unexpected keyword errors).
    - Normalizes common aliases (side/direction, symbol/ticker/pair, band/entry_band/range/etc.).
    - Keeps a copy of all extras in .extras for debugging.
    """

    # Known canonical attributes (set defaults for IDEs/type hints)
    msg_id: Optional[int] = None
    message_id: Optional[int] = None
    id: Optional[int] = None

    side: str = ""               # "LONG" | "SHORT"
    symbol: str = ""             # "ETH/USD" etc.

    # Band (tuple) or scalar low/high variants
    band: Optional[Tuple[float, float]] = None
    band_low: Optional[float] = None
    band_high: Optional[float] = None

    # Risk/targets
    stop_loss: Optional[float] = None
    tp_count: Optional[int] = None

    # Extras (accepted but not required)
    leverage: Optional[int] = None
    timeframe: str = ""

    # Parsed extras live here
    extras: dict

    def __init__(self, **kwargs: Any) -> None:
        # Accept everything
        self.extras = dict(kwargs)

        # --- IDs ----------------------------------------------------------------
        self.msg_id = self._pop_int(kwargs, "msg_id", "message_id", "id")

        # --- Side / Symbol ------------------------------------------------------
        self.side = (self._pop_str(kwargs, "side", "direction") or "").upper()
        self.symbol = self._pop_str(kwargs, "symbol", "ticker", "pair") or ""

        # --- Band (tuple) variants ---------------------------------------------
        band_tuple = self._pop_tuple2(
            kwargs,
            "band", "entry_band", "entry", "range", "price_band", "band_bounds"
        )
        # Scalar low/high variants
        band_low = self._pop_float(
            kwargs,
            "band_low", "entry_low", "range_low", "lower_band",
            "min_price", "low", "lo", "min",
        )
        band_high = self._pop_float(
            kwargs,
            "band_high", "entry_high", "range_high", "upper_band",
            "max_price", "high", "hi", "max",
        )
        if band_tuple is None and band_low is not None and band_high is not None:
            band_tuple = (float(band_low), float(band_high))
        self.band = band_tuple
        self.band_low = float(band_low) if band_low is not None else None
        self.band_high = float(band_high) if band_high is not None else None

        # --- Stop loss (handle many aliases, including uppercase "SL") ----------
        self.stop_loss = self._pop_float(kwargs, "stop_loss", "sl", "SL", "stop", "stopPrice")

        # --- TP count & explicit targets ---------------------------------------
        self.tp_count = self._pop_int(kwargs, "tp_count", "tpn", "tpN", "take_profit_count")

        # These lists are accepted but not required; broker ignores them if present
        self.tps = self._pop_list(kwargs, "tps", "targets", "take_profits", "tp_prices")  # type: ignore[attr-defined]

        # --- Leverage / timeframe ----------------------------------------------
        self.leverage = self._pop_int(kwargs, "leverage", "lev", "x")
        self.timeframe = self._pop_str(kwargs, "timeframe", "tf") or ""

        # Keep any remaining fields in .extras
        self.extras.update(kwargs)

    # ---------- helpers to pop typed values from kwargs ----------
    def _pop_str(self, d: dict, *keys: str) -> Optional[str]:
        for k in keys:
            if k in d and d[k] is not None:
                v = d.pop(k)
                try:
                    return str(v)
                except Exception:
                    return None
        return None

    def _pop_int(self, d: dict, *keys: str) -> Optional[int]:
        for k in keys:
            if k in d and d[k] is not None:
                v = d.pop(k)
                try:
                    if isinstance(v, bool):
                        continue
                    return int(v)
                except Exception:
                    try:
                        s = str(v)
                        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                            return int(s)
                    except Exception:
                        pass
        return None

    def _pop_float(self, d: dict, *keys: str) -> Optional[float]:
        for k in keys:
            if k in d and d[k] is not None:
                v = d.pop(k)
                try:
                    return float(v)
                except Exception:
                    try:
                        return float(str(v))
                    except Exception:
                        pass
        return None

    def _pop_tuple2(self, d: dict, *keys: str) -> Optional[Tuple[float, float]]:
        for k in keys:
            if k in d and d[k] is not None:
                v = d.pop(k)
                try:
                    if isinstance(v, (tuple, list)) and len(v) == 2:
                        a, b = v
                        return float(a), float(b)
                except Exception:
                    pass
        return None

    def _pop_list(self, d: dict, *keys: str) -> Optional[List[Any]]:
        for k in keys:
            if k in d and d[k] is not None:
                v = d.pop(k)
                if isinstance(v, list):
                    return v
                # allow comma-separated strings
                if isinstance(v, str):
                    parts = [p.strip() for p in v.split(",")]
                    return parts
        return None

    def __repr__(self) -> str:
        # Build a concise string like your logs
        band_txt = None
        if self.band is not None:
            try:
                band_txt = f"({float(self.band[0]):.2f}, {float(self.band[1]):.2f})"
            except Exception:
                band_txt = str(self.band)

        parts = []
        head = " ".join([p for p in [self.side, self.symbol] if p])
        if head:
            parts.append(head)
        if band_txt:
            parts.append(f"band={band_txt}")
        if self.stop_loss is not None:
            parts.append(f"SL={self.stop_loss}")
        if self.tp_count is not None:
            parts.append(f"TPn={self.tp_count}")
        if self.leverage is not None:
            parts.append(f"lev={self.leverage}")
        if self.timeframe:
            parts.append(f"TF={self.timeframe}")
        return f"<ExecSignal {' '.join(parts)}>"


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
    # Attribute or attribute-like access
    for name in ("msg_id", "message_id", "id"):
        try:
            if hasattr(sig, name):
                val = getattr(sig, name)
                if isinstance(val, int):
                    return val
                if isinstance(val, str) and (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
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
                    if isinstance(val, str) and (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
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
    Best-effort summary line for logs. Reads both canonical attrs and extras.
    """
    def read_any(obj: Any, *names, default=None):
        for n in names:
            # attribute
            if hasattr(obj, n):
                try:
                    v = getattr(obj, n)
                    if v is not None:
                        return v
                except Exception:
                    pass
            # dict-like
            if isinstance(obj, dict) and n in obj and obj[n] is not None:
                return obj[n]
            # ExecSignal extras
            if hasattr(obj, "extras") and isinstance(obj.extras, dict) and n in obj.extras and obj.extras[n] is not None:
                return obj.extras[n]
        return default

    side = (read_any(sig, "side", "direction") or "").upper()
    symbol = read_any(sig, "symbol", "ticker", "pair") or ""

    band = read_any(sig, "band", "entry_band", "entry", "range", "price_band", "band_bounds")
    if band is None:
        lo = read_any(sig, "band_low", "entry_low", "range_low", "lower_band", "min_price", "low", "lo", "min")
        hi = read_any(sig, "band_high", "entry_high", "range_high", "upper_band", "max_price", "high", "hi", "max")
        if lo is not None and hi is not None:
            band = (float(lo), float(hi))

    sl = read_any(sig, "stop_loss", "sl", "SL", "stop", "stopPrice")
    tpn = read_any(sig, "tp_count", "tpn", "tpN", "take_profit_count")
    lev = read_any(sig, "leverage", "lev", "x")
    tf = read_any(sig, "timeframe", "tf") or ""

    core = []
    if side:
        core.append(side)
    if symbol:
        core.append(symbol)
    head = " ".join(core) if core else "signal"

    parts = []
    if band is not None:
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
    # Test: uppercase SL and extra tps
    s = ExecSignal(
        msg_id=1,
        side="SHORT",
        symbol="ETH/USD",
        entry_band=(3875.33, 3877.16),
        SL=3899.68,                     # uppercase alias
        tpn=6,
        tps=[3870, 3865, 3860],         # extra field accepted
        lev=20,
        tf="5m",
    )
    try:
        execute_signal(s)
    except Exception:
        pass
