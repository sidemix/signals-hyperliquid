# parser.py
import re
from typing import Optional, Tuple, List
from execution import ExecSignal

# Examples supported (flexible):
# "LONG ETH/USD band=(3875.33, 3877.16) SL=3899.68 TPn=6 lev=20.0 TF=5m"
# VIP template you post in Discord also works.

ENTRY_BAND_RE = re.compile(
    r"(?P<side>LONG|SHORT)\s+(?P<symbol>[A-Z]+\/USD).*?(\bband=?\s*\(|Entry\s*Price.*?:)\s*(?P<lo>\d+(\.\d+)?)[^\d]+(?P<hi>\d+(\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
SL_RE = re.compile(r"(?:SL|Stop\s*Loss|StopLoss)\s*[:=]?\s*(?P<sl>\d+(\.\d+)?)", re.IGNORECASE)
LEV_RE = re.compile(r"(?:lev(?:erage)?|x)\s*[:=]?\s*(?P<lev>\d+(\.\d+)?)", re.IGNORECASE)
TF_RE = re.compile(r"\bTF\s*[:=]?\s*(?P<tf>[0-9a-zA-Z]+)", re.IGNORECASE)
TPS_RE = re.compile(r"(?:(?:Targets?.*?:)|TPs?.*?:)\s*(?P<body>[\s\S]+)", re.IGNORECASE)

def _parse_targets(text: str) -> Optional[List[float]]:
    # grabs up to 8 numbers after the targets header
    m = TPS_RE.search(text)
    if not m:
        return None
    body = m.group("body")
    nums = re.findall(r"(\d+\.\d+|\d+)", body)
    vals = []
    for n in nums[:8]:
        try:
            vals.append(float(n))
        except:
            pass
    return vals or None

def parse_signal(text: str) -> Optional[ExecSignal]:
    m = ENTRY_BAND_RE.search(text)
    if not m:
        return None
    side = m.group("side").upper()
    symbol = m.group("symbol").upper()
    lo = float(m.group("lo"))
    hi = float(m.group("hi"))
    slm = SL_RE.search(text)
    levm = LEV_RE.search(text)
    tfm = TF_RE.search(text)
    tps = _parse_targets(text)

    sl = float(slm.group("sl")) if slm else None
    lev = float(levm.group("lev")) if levm else None
    tf = tfm.group("tf") if tfm else None

    return ExecSignal(
        side=side,
        symbol=symbol,
        entry_low=min(lo, hi),
        entry_high=max(lo, hi),
        stop_loss=sl,
        leverage=lev,
        tps=tps,
        timeframe=tf,
        uid=None,
    )
