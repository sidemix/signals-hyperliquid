# parser.py
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------- public API ----------

@dataclass
class ParsedSignal:
    side: str                    # "LONG" | "SHORT"
    symbol: str                  # e.g., "BTC/USD"
    entry_band: Tuple[float, float]  # (low, high)
    stop_loss: Optional[float]       # may be None
    take_profits: List[float]        # may be empty
    leverage: Optional[float]        # numeric if present
    timeframe: Optional[str]         # e.g., "5m"

def parse_signal(raw: str) -> Optional[ParsedSignal]:
    """
    Returns ParsedSignal or None if we can't recognize a trading signal.
    The parser is tolerant to:
      - EN/EM dashes (– —), weird spaces, thousands separators
      - 'StopLoss'/'Stop Loss'/'SL'
      - 'Entry Price'/'Entry' ranges with either '-' or '–'
      - Any UPPERCASE coin like ABC/XYZ, ABC/USD, ABCUSDT (will map to ABC/USD)
    """
    txt = _normalize(raw)

    # Fast fail: must contain LONG/SHORT and an entry range
    side = _extract_side(txt)
    if not side:
        return None

    symbol = _extract_symbol(txt)
    if not symbol:
        return None

    entry_band = _extract_entry_band(txt)
    if not entry_band:
        return None

    stop_loss = _extract_stop_loss(txt)
    tps = _extract_tps(txt)
    lev = _extract_leverage(txt)
    tf = _extract_timeframe(txt)

    return ParsedSignal(
        side=side,
        symbol=symbol,
        entry_band=entry_band,
        stop_loss=stop_loss,
        take_profits=tps,
        leverage=lev,
        timeframe=tf,
    )

# ---------- helpers ----------

def _normalize(s: str) -> str:
    # unify dashes, spaces; strip code blocks; drop emojis we know
    s = s.replace("—", "-").replace("–", "-")  # em/en dash -> hyphen
    s = s.replace("\u00a0", " ")               # nbsp -> space
    # remove thousands separators like 108,240.0 -> 108240.0
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    # normalize labels
    s = re.sub(r"\b(stop\s*loss|stoploss|sl)\b", "SL", s, flags=re.I)
    s = re.sub(r"\bleverage\b", "Leverage", s, flags=re.I)
    s = re.sub(r"\bentry price\b", "Entry Price", s, flags=re.I)
    s = re.sub(r"\btargets?\b", "Targets", s, flags=re.I)
    s = re.sub(r"\btime\s*frame\b", "TF", s, flags=re.I)
    # collapse multiple spaces
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def _extract_side(txt: str) -> Optional[str]:
    m = re.search(r"\b(LONG|SHORT)\b", txt, re.I)
    return m.group(1).upper() if m else None

_SYMBOL_PATTERNS = [
    # Name: ABC/USD or BTC/USD, ETH/USDT, etc.
    re.compile(r"\bName:\s*([A-Z0-9]{2,})\s*/\s*([A-Z]{3,4})\b"),
    # Also accept just COIN/USD outside of Name line
    re.compile(r"\b([A-Z0-9]{2,})\s*/\s*([A-Z]{3,4})\b"),
    # Accept COINUSDT or COINUSD as fallback (map to COIN/USD)
    re.compile(r"\b([A-Z0-9]{2,})(USDT|USD)\b"),
]

def _extract_symbol(txt: str) -> Optional[str]:
    for pat in _SYMBOL_PATTERNS:
        m = pat.search(txt)
        if m:
            coin = m.group(1)
            quote = m.group(2) if m.lastindex and m.lastindex >= 2 else "USD"
            return f"{coin}/{quote.upper()}"
    return None

def _extract_entry_band(txt: str) -> Optional[Tuple[float, float]]:
    # Examples we support:
    #   Entry Price (USD): 1.7042 - 1.7054
    #   Entry Price: 0.091018 - 0.091105
    #   Entry: 108050 - 108100
    # We also accept a single price like "Entry Price: 3840" -> (3840, 3840)
    m = re.search(
        r"\bEntry(?: Price)?(?:\s*\(USD\))?\s*:\s*([0-9]*\.?[0-9]+)\s*(?:-|to)\s*([0-9]*\.?[0-9]+)",
        txt, re.I
    )
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        return (min(low, high), max(low, high))
    m2 = re.search(r"\bEntry(?: Price)?(?:\s*\(USD\))?\s*:\s*([0-9]*\.?[0-9]+)\b", txt, re.I)
    if m2:
        p = float(m2.group(1))
        return (p, p)
    return None

def _extract_stop_loss(txt: str) -> Optional[float]:
    m = re.search(r"\b(?:SL|Stop\s*Loss)\s*[:=]\s*([0-9]*\.?[0-9]+)\b", txt, re.I)
    return float(m.group(1)) if m else None

def _extract_tps(txt: str) -> List[float]:
    # Accept numbered targets or a comma/space list after "Targets"
    # 1) numbered:
    numbered = re.findall(
        r"(?:^|\n|\r)\s*(?:\d+[\)\. ]\s*)?([0-9]*\.?[0-9]+)\s*(?:$|\n|\r)",
        _slice_after("Targets", txt),
        flags=re.I
    )
    # Filter impossible short lines (like "5m")
    vals = [float(x) for x in numbered if re.match(r"^\d", x)]
    # Remove duplicates while preserving order
    out: List[float] = []
    seen = set()
    for v in vals:
        if v not in seen:
            out.append(v); seen.add(v)
    return out

def _extract_leverage(txt: str) -> Optional[float]:
    m = re.search(r"Leverage\s*:\s*(?:Cross|Isolated)?\s*\(?\s*([0-9]+)\s*x\)?", txt, re.I)
    return float(m.group(1)) if m else None

def _extract_timeframe(txt: str) -> Optional[str]:
    m = re.search(r"\bTF\s*:\s*([0-9]+[mhdw])\b", txt, re.I)
    return m.group(1).lower() if m else None

def _slice_after(label: str, txt: str) -> str:
    m = re.search(label, txt, re.I)
    if not m:
        return ""
    return txt[m.end():]
