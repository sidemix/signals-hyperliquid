import re
from typing import List, Optional, Tuple
from pydantic import BaseModel

class Signal(BaseModel):
    symbol: str                      # e.g., "BTC/USD"
    side: str                        # "LONG" | "SHORT"
    entry_band: Tuple[float, float]  # (low, high)
    stop: float
    take_profits: List[float]
    leverage: Optional[float] = None
    timeframe: Optional[str] = None

# -------- regexes ----------
SYMBOL_RE = re.compile(r"(?:Name:|^)\s*([A-Z0-9/]{3,20})")
SIDE_RE   = re.compile(r"\b(LONG|SHORT)\b", re.I)
ENTRY_RE  = re.compile(r"Entry\s*Price.*?([\d,\.]+)\s*[–\-]\s*([\d,\.]+)", re.I)
STOP_RE   = re.compile(r"(?:Stop|StopLoss)\s*[:\-]?\s*([\d,\.]+)", re.I)
# Accept either "TP:" style or "Targets in USD:" blocks
TPS_INLINE_RE = re.compile(r"TPs?\s*[:\-]?\s*([0-9\.,\s]+)", re.I)
TARGETS_BLOCK_RE = re.compile(r"Targets\s+in\s+USD\s*:\s*(.+)", re.I | re.DOTALL)
TF_RE     = re.compile(r"\bTF\s*[:\-]?\s*([0-9a-zA-Z]+)")
LEV_RE    = re.compile(r"Lev(?:erage)?\s*[:\-]?\s*(?:Cross\s*)?\(?\s*([0-9]+)\s*x\)?", re.I)

def _num(s: str) -> float:
    return float(s.replace(",", "").strip())

def _grab_targets(text: str) -> Optional[List[float]]:
    # 1) Inline "TP: 1,2,3" style
    m = TPS_INLINE_RE.search(text)
    if m:
        vals = [v for v in re.findall(r"[\d,\.]+", m.group(1)) if v.strip()]
        return [_num(v) for v in vals]

    # 2) “Targets in USD:” block with each target on its own line
    m = TARGETS_BLOCK_RE.search(text)
    if m:
        block = m.group(1)
        # take lines until we hit a blank line or a line that starts a new section
        lines = []
        for line in block.splitlines():
            if not line.strip():
                break
            if any(h in line.lower() for h in ("stop", "tf", "timeframe", "lev", "leverage", "entry")):
                break
            # pull the first number on the line
            n = re.search(r"([\d,\.]+)", line)
            if n:
                lines.append(_num(n.group(1)))
        return lines if lines else None
    return None

def _norm_symbol(s: str) -> str:
    s = s.strip().upper().replace("USDTUSDT", "USDT")
    # Your signals are “…/USD”; keep it. If someone posts BTCUSDT, normalize.
    if "/" not in s and s.endswith("USD"):
        return f"{s[:-3]}/USD"
    if "/" not in s and s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    return s

def parse_signal_from_text(text: str) -> Optional[Signal]:
    sym_m = SYMBOL_RE.search(text)
    side_m = SIDE_RE.search(text)
    entry_m = ENTRY_RE.search(text)
    stop_m = STOP_RE.search(text)
    tps = _grab_targets(text)
    lev_m = LEV_RE.search(text)
    tf_m = TF_RE.search(text)

    if not (sym_m and side_m and entry_m and stop_m and tps):
        return None

    symbol = _norm_symbol(sym_m.group(1))
    side = side_m.group(1).upper()
    e1, e2 = _num(entry_m.group(1)), _num(entry_m.group(2))
    stop = _num(stop_m.group(1))
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
