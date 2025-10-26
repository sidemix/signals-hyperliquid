# parser.py
import re
import logging
from execution import ExecSignal   # <-- use the unified dataclass

log = logging.getLogger("parser")

# Regexes robust to hyphen or en-dash and flexible spacing/case
_ENTRY_RE = re.compile(
    r"Entry\s*Price\s*\(.*?\)\s*[:\-]\s*(?P<low>\d+(?:\.\d+)?)\s*[–-]\s*(?P<high>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SYMBOL_RE = re.compile(r"Name\s*[:\-]\s*(?P<sym>[A-Z0-9/]+)", re.IGNORECASE)
_SIDE_RE   = re.compile(r"\b(Long|Short)\b", re.IGNORECASE)
_SL_RE     = re.compile(r"(?:Stop\s*Loss|StopLoss|SL)\s*[:\-]\s*(?P<sl>\d+(?:\.\d+)?)", re.IGNORECASE)
_LEV_RE    = re.compile(r"Leverage\s*[:\-]\s*(?:Cross|Isolated)?\s*\(?(?P<lev>\d+(?:\.\d+)?)x\)?", re.IGNORECASE)
_TF_RE     = re.compile(r"\bTF\s*[:\-]\s*(?P<tf>\d+\s*(?:m|h|d|w|min|mins|minute|minutes|hr|hour|hours))\b", re.IGNORECASE)

def _norm_tf(tf: str | None) -> str | None:
    if not tf:
        return None
    t = tf.lower().replace(" ", "")
    t = t.replace("mins", "m").replace("min", "m")
    t = t.replace("hours", "h").replace("hour", "h").replace("hr", "h")
    return t  # e.g., "5m", "1h"

def parse_signal(text: str) -> ExecSignal | None:
    if not text or len(text) < 10:
        return None

    side_m  = _SIDE_RE.search(text)
    sym_m   = _SYMBOL_RE.search(text)
    entry_m = _ENTRY_RE.search(text)

    if not (side_m and sym_m and entry_m):
        log.info("[PARSER] Missing required field(s): side=%s sym=%s entry=%s",
                 bool(side_m), bool(sym_m), bool(entry_m))
        return None

    side = "LONG" if side_m.group(1).lower().startswith("l") else "SHORT"
    symbol = sym_m.group("sym").upper()
    entry_low = float(entry_m.group("low"))
    entry_high = float(entry_m.group("high"))

    sl = float(_SL_RE.search(text).group("sl")) if _SL_RE.search(text) else None
    lev = float(_LEV_RE.search(text).group("lev")) if _LEV_RE.search(text) else None
    tf  = _norm_tf(_TF_RE.search(text).group("tf")) if _TF_RE.search(text) else None

    sig = ExecSignal(
        side=side,
        symbol=symbol,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=sl,
        leverage=lev,
        timeframe=tf,
        tif=None,              # let broker default from env
        notional_usd=None,     # let broker default from env
    )

    log.info(
        "[PARSER] Parsed signal: %s %s band=(%.2f–%.2f) sl=%s lev=%s tf=%s",
        side, symbol, entry_low, entry_high, sl, lev, tf,
    )
    return sig
