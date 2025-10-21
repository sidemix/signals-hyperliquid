import re
from typing import List, Optional, Tuple
from pydantic import BaseModel

class Signal(BaseModel):
    symbol: str                      # e.g., "ETH/USD"
    side: str                        # "LONG" | "SHORT"
    entry_band: Tuple[float, float]  # (low, high)
    stop: float
    take_profits: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None

# --- Robust patterns ---
# 1) Prefer: "Name: ETH/USD" (or USDT)
NAME_LINE_RE = re.compile(
    r"(?:^|\n)\s*Name\s*:\s*([A-Z0-9]{2,15})\s*/\s*(USDT|USD)\b",
    re.IGNORECASE,
)

# 2) Fallback: any AAA/USDT or AAA/USD token in the text
INLINE_PAIR_RE = re.compile(
    r"\b([A-Z0-9]{2,15})/(USDT|USD)\b",
    re.IGNORECASE,
)

SIDE_RE   = re.compile(r"\b(LONG|SHORT)\b", re.I)
ENTRY_RE  = re.compile(r"Entry.*?([\d\.]+)\s*[–\-]\s*([\d\.]+)", re.I)
STOP_RE   = re.compile(r"(?:Stop|StopLoss)\s*[:\-]?\s*([\d\.]+)", re.I)
# Accept "Targets in USD: 3863.16, 3853.43, ..." OR "Targets: ..."
TPS_RE    = re.compile(r"Targets(?:\s+in\s+USD)?\s*[:\-]?\s*([0-9\.,\s]+)", re.I)
TF_RE     = re.compile(r"\bTF\s*[:\-]?\s*([0-9a-zA-Z]+)\b")
LEV_RE    = re.compile(r"Lev(?:erage)?\s*[:\-]?\s*(?:Cross\s*)?\(?\s*([0-9]+)\s*x\)?", re.I)

def _norm_symbol(base: str, quote: str) -> str:
    return f"{base.upper()}/{quote.upper()}"

def _extract_symbol(text: str) -> Optional[str]:
    # 1) Try explicit Name: line first
    m = NAME_LINE_RE.search(text)
    if m:
        return _norm_symbol(m.group(1), m.group(2))
    # 2) Else take the first inline token that looks like a proper pair
    m = INLINE_PAIR_RE.search(text)
    if m:
        return _norm_symbol(m.group(1), m.group(2))
    return None

def parse_signal_from_text(text: str) -> Optional[Signal]:
    symbol = _extract_symbol(text or "")
    side_m = SIDE_RE.search(text or "")
    entry_m = ENTRY_RE.search(text or "")
    stop_m = STOP_RE.search(text or "")
    tps_m = TPS_RE.search(text or "")
    lev_m = LEV_RE.search(text or "")
    tf_m = TF_RE.search(text or "")

    if not (symbol and side_m and entry_m and stop_m and tps_m):
        return None

    side = side_m.group(1).upper()
    e1, e2 = float(entry_m.group(1)), float(entry_m.group(2))
    stop = float(stop_m.group(1))
    # extract floats from the targets list
    tps = [float(x) for x in re.findall(r"[\d\.]+", tps_m.group(1))]
    tf = tf_m.group(1) if tf_m else None
    lev = float(lev_m.group(1)) if lev_m else None

    return Signal(
        symbol=symbol,
        side=side,
        entry_band=(min(e1, e2), max(e1, e2)),
        stop=stop,
        take_profits=tps,
        leverage=lev,
        timeframe=tf,
    )
