# parser.py
import re
import logging
from dataclasses import dataclass

log = logging.getLogger("parser")

@dataclass
class ExecSignal:
    side: str             # "LONG" or "SHORT"
    symbol: str           # e.g. "BTC/USD"
    entry_low: float
    entry_high: float
    stop_loss: float | None = None
    leverage: float | None = None
    tif: str | None = None
    notional_usd: float | None = None

# Matches numbers like 111048.32 – 111063.61 or 111048.32 - 111063.61
_ENTRY_RE = re.compile(
    r"Entry\s*Price\s*\(.*?\)\s*[:\-]\s*(?P<low>\d+(?:\.\d+)?)\s*[–-]\s*(?P<high>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

_SYMBOL_RE = re.compile(r"Name\s*[:\-]\s*(?P<sym>[A-Z0-9/]+)", re.IGNORECASE)
_SIDE_RE = re.compile(r"\b(Long|Short)\b", re.IGNORECASE)
_SL_RE = re.compile(r"Stop\s*Loss\s*[:\-]\s*(?P<sl>\d+(?:\.\d+)?)", re.IGNORECASE)
_LEV_RE = re.compile(r"Leverage\s*[:\-]\s*(?:Cross\s*\()?(?P<lev>\d+(?:\.\d+)?)x", re.IGNORECASE)

def parse_signal(text: str) -> ExecSignal | None:
    """Extract fields from a Discord signal message."""
    if not text or len(text) < 10:
        return None

    side_m = _SIDE_RE.search(text)
    symbol_m = _SYMBOL_RE.search(text)
    entry_m = _ENTRY_RE.search(text)
    sl_m = _SL_RE.search(text)
    lev_m = _LEV_RE.search(text)

    if not side_m or not symbol_m or not entry_m:
        log.info("[PARSER] Missing required field: side=%s sym=%s entry=%s",
                 bool(side_m), bool(symbol_m), bool(entry_m))
        return None

    side = side_m.group(1).upper()
    symbol = symbol_m.group("sym").upper()
    entry_low = float(entry_m.group("low"))
    entry_high = float(entry_m.group("high"))
    stop_loss = float(sl_m.group("sl")) if sl_m else None
    leverage = float(lev_m.group("lev")) if lev_m else None

    sig = ExecSignal(
        side=side,
        symbol=symbol,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        leverage=leverage,
    )

    log.info(
        "[PARSER] Parsed signal: %s %s band=(%.2f–%.2f) sl=%s lev=%s",
        side, symbol, entry_low, entry_high, stop_loss, leverage,
    )
    return sig
