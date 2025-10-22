# execution.py
from __future__ import annotations

import importlib
import logging
import traceback
import hashlib
from collections import deque
from typing import Any, Optional, Tuple, List, Iterable, Iterator

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# -----------------------------------------------------------------------------
# ExecSignal: robust, dict-like container that accepts ANY kwargs and normalizes aliases
# -----------------------------------------------------------------------------
class ExecSignal:
    """
    Flexible container for parsed trade signals.

    - Accepts ANY kwargs from upstream parser.
    - Normalizes common aliases (side/direction, symbol/ticker/pair, band/entry_band/...).
    - Exposes canonical fields as attributes AND as a dict-like mapping:
        sig["stop_loss"], sig.get("stop_loss"), "stop_loss" in sig, for broker compatibility.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Keep original inputs
        self._extras = dict(kwargs)

        # ---- IDs --------------------------------------------------------------
        self.msg_id = self._pop_int(kwargs, "msg_id", "message_id", "id")

        # ---- Side / Symbol ----------------------------------------------------
        self.side = (self._pop_str(kwargs, "side", "direction") or "").upper()
        self.symbol = self._pop_str(kwargs, "symbol", "ticker", "pair") or ""

        # ---- Band (tuple) variants -------------------------------------------
        band_tuple = self._pop_tuple2(
            kwargs, "band", "entry_band", "entry", "range", "price_band", "band_bounds"
        )
        # Scalar low/high variants
        band_low = self._pop_float(
            kwargs, "band_low", "entry_low", "range_low", "lower_band",
            "min_price", "low", "lo", "min",
        )
        band_high = self._pop_float(
            kwargs, "band_high", "entry_high", "range_high", "upper_band",
            "max_price", "high", "hi", "max",
        )
        if band_tuple is None and band_low is not None and band_high is not None:
            band_tuple = (float(band_low), float(band_high))
        self.band = band_tuple
        self.band_low = float(band_low) if band_low is not None else None
        self.band_high = float(band_high) if band_high is not None else None

        # ---- Stop loss (handle many aliases, including uppercase "SL") --------
        self.stop_loss = self._pop_float(kwargs, "stop_loss", "sl", "SL", "stop", "stopPrice")

        # ---- TP count & optional explicit targets ----------------------------
        self.tp_count = self._pop_int(kwargs, "tp_count", "tpn", "tpN", "take_profit_count")
        self.tps = self._pop_list(kwargs, "tps", "targets", "take_profits", "tp_prices")  # optional list

        # ---- Leverage / timeframe --------------------------------------------
        self.leverage = self._pop_int(kwargs, "leverage", "lev", "x")
        self.timeframe = self._pop_str(kwargs, "timeframe", "tf") or ""

        # Anything left stays in extras (and will be visible via mapping)
        self._extras.update(kwargs)

        # ---- Build a canonical mapping that the broker can read like a dict ---
        self._map = {
            # ids
            "msg_id": self.msg_id,
            "message_id": self.msg_id,
            "id": self.msg_id,
            # core
            "side": self.side,
            "symbol": self.symbol,
            # band
            "band": self.band,
            "band_low": self.band_low,
            "band_high": self.band_high,
            # risk/targets
            "stop_loss": self.stop_loss,
            "sl": self.stop_loss,
            "SL": self.stop_loss,
            "tp_count": self.tp_count,
            "tpn": self.tp_count,
            "tpN": self.tp_count,
            "take_profit_count": self.tp_count,
            # extras
            "leverage": self.leverage,
            "lev": self.leverage,
            "x": self.leverage,
            "timeframe": self.timeframe,
            "tf": self.timeframe,
            "tps": self.tps,
        }
        # Merge any remaining original fields without trampling canonicals
        for k, v in self._extras.items():
            self._map.setdefault(k, v)

    # ---------- dict-like interface so broker can use sig[...] / sig.get(...) ----------
    def __getitem__(self, key: str) -> Any:
        return self._map[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._map.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._map

    def keys(self) -> Iterable[str]:
        return self._map.keys()

    def items(self) -> Iterable[tuple[str, Any]]:
        return self._map.items()

    def values(self) -> Iterable[Any]:
        return self._map.values()

    def __iter__(self) -> Iterator[str]:
        return iter(self._map)

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
                if isinstance(v, str):
                    return [p.strip() for p in v.split(",")]
        return None

    # ---------- pretty repr for your logs ----------
    def __repr__(self) -> str:
        band_txt = None
        if self.band is not None:
            try:
                band_txt = f"({float(self.band[0]):.6f}, {float(self.band[1]):.6f})"
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
# Dedupe for message IDs (prevents double-runs on the same signal)
# -----------------------------------------------------------------------------
_SEEN_IDS: set[str] = set()
_SEEN_ORDER = deque(maxlen=800)  # rolling window

def _stringify_id(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (int,)):
        return str(val)
    s = str(val).strip()
    return s or None

def _get_msg_id(sig: Any) -> Optional[str]:
    # attribute first
    for name in ("msg_id", "message_id", "id"):
        try:
            if hasattr(sig, name):
                v = getattr(sig, name)
                s = _stringify_id(v)
                if s:
                    return s
        except Exception:
            pass
    # mapping style
    if isinstance(sig, dict) or hasattr(sig, "get"):
        for name in ("msg_id", "message_id", "id"):
            try:
                v = sig.get(name)  # type: ignore[attr-defined]
                s = _stringify_id(v)
                if s:
                    return s
            except Exception:
                pass
    return None

def _fingerprint(sig: Any) -> str:
    """
    Deterministic content fingerprint used when no message id is available.
    Prefer a raw message/content field if present; else repr of canonical map if available.
    """
    raw = None
    for k in ("raw_text", "content", "message_text"):
        if hasattr(sig, k):
            try:
                vv = getattr(sig, k)
                if vv:
                    raw = str(vv)
                    break
            except Exception:
                pass
        if isinstance(sig, dict) and sig.get(k):
            raw = str(sig[k])
            break

    if raw is None:
        try:
            # If ExecSignal, use its mapping; else use repr(sig)
            if hasattr(sig, "items"):
                raw = repr(sorted(list(sig.items())))  # type: ignore[attr-defined]
            else:
                raw = repr(sig)
        except Exception:
            raw = str(sig)

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def _already_processed(uid: str) -> bool:
    if uid in _SEEN_IDS:
        print(f"[SKIP] duplicate uid={uid}")
        return True
    _SEEN_IDS.add(uid)
    _SEEN_ORDER.append(uid)
    if len(_SEEN_IDS) > _SEEN_ORDER.maxlen:
        old = _SEEN_ORDER.popleft()
        _SEEN_IDS.discard(old)
    return False

# -----------------------------------------------------------------------------
# Broker loader
# -----------------------------------------------------------------------------
def _get_broker_submit():
    try:
        mod = importlib.import_module("broker.hyperliquid")
        if not hasattr(mod, "submit_signal"):
            raise AttributeError("broker.hyperliquid has no submit_signal()")
        return mod.submit_signal
    except Exception:
        tb = traceback.format_exc()
        raise RuntimeError(f"Broker import failed:\n{tb}")

# -----------------------------------------------------------------------------
# Pretty summaries for logs
# -----------------------------------------------------------------------------
def _summarize(sig: Any) -> str:
    def read_any(obj: Any, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                try:
                    v = getattr(obj, n)
                    if v is not None:
                        return v
                except Exception:
                    pass
            if isinstance(obj, dict) and n in obj and obj[n] is not None:
                return obj[n]
            if hasattr(obj, "get"):
                try:
                    v = obj.get(n)  # type: ignore[attr-defined]
                    if v is not None:
                        return v
                except Exception:
                    pass
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
    return f"{head} {' '.join(parts)}".strip()

# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def execute_signal(sig: Any) -> None:
    uid = _get_msg_id(sig) or _fingerprint(sig)
    if _already_processed(uid):
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
    s = ExecSignal(
        msg_id=1,
        side="LONG",
        symbol="ARB/USD",
        entry_band=(0.310381, 0.310702),
        SL=0.306424,          # uppercase supported
        tpn=1,
        lev=20,
        tf="5m",
    )
    try:
        execute_signal(s)
    except Exception:
        pass
