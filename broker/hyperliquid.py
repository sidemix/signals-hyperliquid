"""
broker/hyperliquid.py
---------------------
This module connects your signal executor to the Hyperliquid exchange.

It’s called automatically from execution.py when a signal is parsed.

Implements:
    submit_signal(exec_sig | **kwargs)

Once you fill in the REST or SDK calls below,
it will automatically place OTO brackets (entry, stop, and take-profits).
"""

from __future__ import annotations
import os, math, time, httpx
from typing import Any, Dict, List, Optional, Union

try:
    from execution import ExecSignal  # type hint
except Exception:
    ExecSignal = Any  # fallback if type not found


# ======================================================
# 1. API Hook functions — fill these with your real client
# ======================================================

def _hl_get_markets(base_url: str) -> Dict[str, Any]:
    """
    Fetch market metadata (tick/lot size, decimals, etc.)
    Replace this with your Hyperliquid API or SDK call.
    """
    url = base_url.rstrip("/") + "/info"
    with httpx.Client(timeout=10) as cx:
        r = cx.get(url)
        r.raise_for_status()
        return r.json()


def _hl_place_limit(
    base_url: str,
    coin: str,
    size: float,
    price: float,
    reduce_only: bool,
    tif: str = "Gtc",
    client_id: Optional[str] = None,
) -> None:
    """
    Place a LIMIT order.
    Replace this with your Hyperliquid SDK or REST call.
    """
    # TODO: Replace with real order call.
    print(f"[HL] LIMIT {coin} sz={size:.8f} px={price:.6f} RO={reduce_only} tif={tif} cid={client_id or '-'}")


def _hl_place_trigger(
    base_url: str,
    coin: str,
    size: float,
    trigger_px: float,
    side: str,
    limit_px: Optional[float] = None,
    reduce_only: bool = True,
) -> None:
    """
    Place a stop-market or stop-limit order.
    Replace with your SDK or signed REST call.
    """
    kind = "STOP-LIMIT" if limit_px else "STOP-MARKET"
    print(f"[HL] {kind} {coin} sz={size:.8f} trig={trigger_px:.6f} side={side} RO={reduce_only} lim={limit_px}")


# ======================================================
# 2. Utility functions
# ======================================================

def _coin_from_symbol(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def _market_from_symbol(symbol: str) -> str:
    return f"{_coin_from_symbol(symbol)}-USD"


def _round_px(px: float, px_decimals: int) -> float:
    q = 10 ** px_decimals
    return math.floor(px * q + 0.5) / q


def _round_sz(sz: float, sz_decimals: int) -> float:
    q = 10 ** sz_decimals
    return math.floor(sz * q + 1e-12) / q


def _split_sizes(total: float, n: int) -> List[float]:
    # equal split (can be modified for ladder)
    each = total / n
    return [each] * n


def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


# ======================================================
# 3. Real order placement logic
# ======================================================

def _place_order_real(payload: Dict[str, Any]) -> None:
    """
    Real placement of a bracket:
      - entry LIMIT
      - stop trigger (reduce-only)
      - N TP limits (reduce-only)
    """

    base_url = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/")
    usd_risk = float(os.getenv("TRADE_SIZE_USD", "100"))
    tif = os.getenv("HL_TIF", "Gtc")
    use_stop_limit = os.getenv("HL_STOP_LIMIT", "false").lower() in ("1", "true", "yes", "on")

    symbol = payload["symbol"]
    side = payload["side"].upper()
    low, high = payload["entry_band"]
    stop_px = float(payload["stop"])
    tps = [float(x) for x in payload["tps"]]
    lev = payload.get("leverage")

    coin = _coin_from_symbol(symbol)
    market = _market_from_symbol(symbol)

    # Fetch rounding metadata (decimals)
    info = _hl_get_markets(base_url)
    px_dec, sz_dec = 2, 4
    try:
        px_dec = int(info.get("pxDecimals", {}).get(coin, 2))
        sz_dec = int(info.get("szDecimals", {}).get(coin, 4))
    except Exception:
        pass

    # Entry price: midpoint of band
    entry_px = (low + high) / 2.0
    entry_px = _round_px(entry_px, px_dec)

    # Coin size from USD
    size_coin = usd_risk / entry_px
    size_coin = _round_sz(size_coin, sz_dec)
    if size_coin <= 0:
        raise RuntimeError("Size rounded to zero — increase TRADE_SIZE_USD or check szDecimals.")

    # Sign by direction
    is_long = (side == "LONG")
    signed_size = +size_coin if is_long else -size_coin

    # Optional: set leverage (depends on SDK)
    if lev:
        print(f"[HL] leverage request: {coin} -> {lev}x (implement if supported by your client)")

    # ===== ENTRY ORDER =====
    _hl_place_limit(
        base_url=base_url,
        coin=coin,
        size=signed_size,
        price=entry_px,
        reduce_only=False,
        tif=tif,
        client_id=f"entry-{coin}-{int(time.time())}",
    )

    # ===== STOP LOSS =====
    stop_side = "SELL" if is_long else "BUY"
    stop_px_rounded = _round_px(stop_px, px_dec)
    _hl_place_trigger(
        base_url=base_url,
        coin=coin,
        size=abs(size_coin),
        trigger_px=stop_px_rounded,
        side=stop_side,
        limit_px=_round_px(stop_px_rounded, px_dec) if use_stop_limit else None,
        reduce_only=True,
    )

    # ===== TAKE PROFITS =====
    splits = _split_sizes(abs(size_coin), len(tps))
    for i, (tp_px, sz) in enumerate(zip(tps, splits), start=1):
        tp_px_r = _round_px(tp_px, px_dec)
        sz_r = _round_sz(sz, sz_dec)
        if sz_r <= 0:
            continue
        _hl_place_limit(
            base_url=base_url,
            coin=coin,
            size=(-sz_r if is_long else +sz_r),
            price=tp_px_r,
            reduce_only=True,
            tif="Gtc",
            client_id=f"tp{i}-{coin}-{int(time.time())}",
        )

    print(f"[HL] Bracket placed for {market} — entry {entry_px}, stop {stop_px_rounded}, {len(tps)} TPs.")


# ======================================================
# 4. Normalize + Public Entry (called by execution.py)
# ======================================================

def _normalize_payload(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(sig_or_kwargs, dict):
        symbol = sig_or_kwargs["symbol"]
        side = sig_or_kwargs["side"]
        band = sig_or_kwargs.get("entry_band") or (sig_or_kwargs["entry_low"], sig_or_kwargs["entry_high"])
        stop = float(sig_or_kwargs["stop"])
        tps = list(sig_or_kwargs["tps"])
        lev = sig_or_kwargs.get("leverage")
        tf = sig_or_kwargs.get("timeframe")
    else:
        s = sig_or_kwargs
        symbol, side = s.symbol, s.side
        band = s.entry_band
        stop = float(s.stop)
        tps = list(s.tps)
        lev = s.leverage
        tf = s.timeframe

    low, high = float(band[0]), float(band[1])
    return {
        "symbol": str(symbol).upper(),
        "side": side.upper(),
        "entry_band": (low, high),
        "stop": stop,
        "tps": tps,
        "leverage": lev,
        "timeframe": tf,
    }


def _log_preview(payload: Dict[str, Any]) -> None:
    sym = payload["symbol"]
    side = payload["side"]
    low, high = payload["entry_band"]
    print(
        f"[BROKER] {side} {sym} band=({low:.6f},{high:.6f}) "
        f"SL={payload['stop']:.6f} TPn={len(payload['tps'])} "
        f"lev={payload.get('leverage') or 'n/a'} TF={payload.get('timeframe') or 'n/a'}"
    )


def submit_signal(sig_or_kwargs: Union["ExecSignal", Dict[str, Any]], **kw) -> None:
    """
    Main entry point expected by execution.py.
    Accepts ExecSignal or expanded kwargs.
    """
    payload = _normalize_payload(sig_or_kwargs if kw == {} else {**sig_or_kwargs, **kw})  # type: ignore
    _log_preview(payload)

    # Sanity check for credentials
    api_key = os.getenv("HYPER_API_KEY", "")
    api_secret = os.getenv("HYPER_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("Missing HYPER_API_KEY / HYPER_API_SECRET. Please set them in Render.")

    # Execute
    _place_order_real(payload)
