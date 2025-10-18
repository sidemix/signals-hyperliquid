import re
from typing import List, Optional, Tuple
from pydantic import BaseModel

class Signal(BaseModel):
    symbol: str                      # e.g., "BTC/USDT"
    side: str                        # "LONG" | "SHORT"
    entry_band: Tuple[float, float]  # (low, high)
    stop: float
    take_profits: List[float]
    leverage: Optional[float] = None # e.g., 20
    timeframe: Optional[str] = None

SYMBOL_RE = re.compile(r"(?:Name:|^)\s*([A-Z0-9/]{3,20})")
SIDE_RE   = re.compile(r"\b(LONG|SHORT)\b", re.I)
ENTRY_RE  = re.compile(r"Entry.*?([\d\.]+)\s*[–\-]\s*([\d\.]+)", re.I)
STOP_RE   = re.compile(r"(?:Stop|StopLoss)\s*[:\-]?\s*([\d\.]+)", re.I)
TPS_RE    = re.compile(r"TPs?\s*[:\-]?\s*([0-9\.,\s]+)", re.I)
TF_RE     = re.compile(r"TF\s*[:\-]?\s*([0-9a-zA-Z]+)")
LEV_RE    = re.compile(r"Lev(?:erage)?\s*[:\-]?\s*(?:Cross\s*)?\(?\s*([0-9]+)\s*x\)?", re.I)

def _norm_symbol(s: str) -> str:
    s = s.strip().upper().replace("USDTUSDT", "USDT")
    if "/" not in s and s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    return s

def parse_signal_from_text(text: str) -> Optional[Signal]:
    sym_m = SYMBOL_RE.search(text)
    side_m = SIDE_RE.search(text)
    entry_m = ENTRY_RE.search(text)
    stop_m = STOP_RE.search(text)
    tps_m = TPS_RE.search(text)
    lev_m = LEV_RE.search(text)
    tf_m = TF_RE.search(text)

    if not (sym_m and side_m and entry_m and stop_m and tps_m):
        return None

    symbol = _norm_symbol(sym_m.group(1))
    side = side_m.group(1).upper()
    e1, e2 = float(entry_m.group(1)), float(entry_m.group(2))
    stop = float(stop_m.group(1))
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
