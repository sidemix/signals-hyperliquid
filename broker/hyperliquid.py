# broker/hyperliquid.py
from __future__ import annotations

import os
import math
import time
import logging
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ---------------------------------------------------------------------------------
# Optional SDK imports (we'll still run in DRY_RUN if missing or misconfigured)
# ---------------------------------------------------------------------------------
try:
    from hyperliquid.info import Info   # type: ignore
except Exception:  # pragma: no cover
    Info = None  # type: ignore

try:
    from hyperliquid.exchange import Exchange  # type: ignore
except Exception:  # pragma: no cover
    Exchange = None  # type: ignore

# ---------------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------------
RAW_ALLOW = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "*").strip()
if RAW_ALLOW in ("", "*"):
    ALLOW_ALL = True
    ALLOWED_SET = set()
else:
    ALLOW_ALL = False
    ALLOWED_SET = {s.strip().upper() for s in RAW_ALLOW.split(",") if s.strip()}

# Size / risk
NOTIONAL_USD = float(os.getenv("HYPER_NOTIONAL_USD", "50"))  # straightforward fixed notional
DEFAULT_LEV = int(float(os.getenv("HYPER_DEFAULT_LEVERAGE", "20")))
TIF = os.getenv("HYPER_TIF", "Gtc").capitalize()  # Alo | Ioc | Gtc
DRY_RUN = os.getenv("HYPER_DRY_RUN", "true").lower() != "false"

# Network (for Info base_url selection; sdk may handle defaults)
NETWORK = os.getenv("HYPER_NETWORK", "mainnet").lower()  # "mainnet" | "testnet"

# If you want to place real orders via the SDK (you also need to configure signing):
AGENT_PRIVATE_KEY = os.getenv("HYPER_AGENT_PRIVATE_KEY")  # hex key (danger: keep safe!)
VAULT_ADDRESS = os.getenv("HYPER_VAULT_ADDRESS", "")      # optional subaccount/vault


# ---------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------
def _symbol_allowed(symbol: str) -> bool:
    if ALLOW_ALL:
        return True
    return symbol.upper() in ALLOWED_SET


def _symbol_to_coin(symbol: str) -> str:
    """'ETH/USD' -> 'ETH', 'APEX/USD' -> 'APEX'"""
    s = symbol.upper().strip()
    if "/" in s:
        return s.split("/")[0]
    return s


def _get_info() -> Optional[Any]:
    """Construct an Info client if available."""
    if Info is None:
        return None
    try:
        if NETWORK.startswith("test"):
            return Info(base_url="https://api.hyperliquid-testnet.xyz")
        return Info(base_url="https://api.hyperliquid.xyz")
    except Exception as e:
        log.warning(f"[BROKER] Info init failed: {e}")
        return None


def _get_meta(info: Any) -> Optional[Dict[str, Any]]:
    try:
        return info.meta()
    except Exception as e:
        log.warning(f"[BROKER] meta() failed: {e}")
        return None


def _asset_index(info: Any, coin: str) -> Optional[int]:
    """Perps: asset = index in meta['universe'] where coin matches."""
    meta = _get_meta(info)
    if not meta or "universe" not in meta:
        return None
    uni = meta["universe"]
    for idx, u in enumerate(uni):
        try:
            if (u.get("name") or u.get("asset") or u.get("coin") or "").upper() == coin.upper():
                return idx
            # some SDKs keep 'name' as the coin symbol; fallback: compare 'indexSymbol' if present
            if str(u.get("indexSymbol", "")).upper() == coin.upper():
                return idx
        except Exception:
            continue
    return None


def _sz_decimals(info: Any, asset_idx: int) -> Optional[int]:
    meta = _get_meta(info)
    if not meta:
        return None
    try:
        uni = meta["universe"]
        return int(uni[asset_idx]["szDecimals"])
    except Exception:
        return None


def _get_mark_price(coin: str) -> Optional[float]:
    """Try several info calls to get a reasonable mark/mid price for the coin."""
    info = _get_info()
    if info is None:
        log.warning("WARNING:broker.hyperliquid:Info client not available; cannot fetch mark.")
        return None

    # 1) active_asset_ctx (preferred) -> markPx
    try:
        if hasattr(info, "active_asset_ctx"):
            ctx = info.active_asset_ctx(coin)
            if ctx and "ctx" in ctx and "markPx" in ctx["ctx"]:
                return float(ctx["ctx"]["markPx"])
    except Exception as e:
        log.warning(f"WARNING:broker.hyperliquid:active_asset_ctx failed for {coin}: {e}")

    # 2) all_mids -> mids[coin]
    try:
        mids = info.all_mids()
        if mids and isinstance(mids, dict):
            mid = mids.get(coin.upper()) or mids.get(coin.capitalize()) or mids.get(coin)
            if mid is not None:
                return float(mid)
    except Exception as e:
        log.warning(f"WARNING:broker.hyperliquid:all_mids failed for {coin}: {e}")

    # 3) l2Book midpoint
    try:
        book = info.l2_book(coin, nSigFigs=5, mantissa=None) if hasattr(info, "l2_book") else None
        if book and "levels" in book and book["levels"]:
            bids, asks = book["levels"][0], book["levels"][1]
            best_bid = float(bids[0]["px"]) if bids else None
            best_ask = float(asks[0]["px"]) if asks else None
            if best_bid and best_ask:
                return (best_bid + best_ask) / 2.0
    except Exception:
        pass

    return None


def _fmt_px_sz_for_perp(
    info: Optional[Any],
    coin: str,
    px: float,
    sz: float,
) -> Tuple[str, str]:
    """
    Conform to tick/lot constraints:
    - Prices: up to 5 significant figures but at most (6 - szDecimals) decimal places for perps.
    - Sizes: rounded to szDecimals.
    If info unavailable, fall back to 6 dp for px and 3 dp for sz (safe default).
    """
    sz_dec = 3
    if info:
        idx = _asset_index(info, coin)
        dec = _sz_decimals(info, idx) if idx is not None else None
        if isinstance(dec, int):
            sz_dec = dec

    # Size rounding
    sz_rounded = round(float(sz), sz_dec)
    sz_str = f"{sz_rounded:.{sz_dec}f}"

    # Price rounding: max_decimals = 6 - szDecimals (integer prices always fine)
    max_decimals = max(0, 6 - sz_dec)
    if px >= 1:
        # Keep integer if big number; else limit decimals
        px_rounded = round(float(px), max_decimals)
        if px_rounded.is_integer():
            px_str = f"{int(px_rounded)}"
        else:
            px_str = f"{px_rounded:.{max_decimals}f}"
    else:
        # For small prices, just cap decimals to max_decimals (may be many leading zeros)
        px_rounded = round(float(px), max_decimals)
        px_str = f"{px_rounded:.{max_decimals}f}"

    return px_str, sz_str


# ---------------------------------------------------------------------------------
# Signal extraction (works with object or dict-like; execution.py already helps)
# ---------------------------------------------------------------------------------
def _extract_signal(sig: Any) -> Dict[str, Any]:
    """
    Normalize incoming signal into a dict the rest of this module expects.
    Required:
      - side: "LONG" or "SHORT"
      - symbol: e.g. "ETH/USD"
      - entry band: (low, high) tuple (we'll pick a px inside)
      - stop_loss
    Optional:
      - tp_count, leverage, timeframe
    """
    # read helper that tries attribute / mapping
    def read(*names, default=None):
        for n in names:
            if hasattr(sig, n):
                try:
                    v = getattr(sig, n)
                    if v is not None:
                        return v
                except Exception:
                    pass
            if isinstance(sig, dict) and n in sig and sig[n] is not None:
                return sig[n]
            if hasattr(sig, "get"):
                try:
                    v = sig.get(n)  # type: ignore[attr-defined]
                    if v is not None:
                        return v
                except Exception:
                    pass
        return default

    side = (read("side", "direction", default="") or "").upper()
    symbol = read("symbol", "ticker", "pair", default="")
    if not side or not symbol:
        raise ValueError("Signal missing side/symbol.")

    # band
    band = (
        read("band") or read("entry_band") or read("entry") or
        read("range") or read("price_band") or read("band_bounds")
    )
    if band is None:
        lo = read("band_low", "entry_low", "range_low", "lower_band", "min_price", "low", "lo", "min")
        hi = read("band_high", "entry_high", "range_high", "upper_band", "max_price", "high", "hi", "max")
        if lo is not None and hi is not None:
            band = (float(lo), float(hi))
    if not band or not isinstance(band, (tuple, list)) or len(band) != 2:
        raise ValueError("Signal missing band_low/band_high (or equivalent fields).")

    stop_loss = read("stop_loss", "sl", "SL", "stop", "stopPrice")
    if stop_loss is None:
        raise ValueError("Signal missing stop_loss/SL.")

    lev = read("leverage", "lev", "x", default=None)
    tpn = read("tp_count", "tpn", "tpN", "take_profit_count", default=None)
    tf = read("timeframe", "tf", default=None)

    return {
        "side": side,
        "symbol": symbol,
        "band": (float(band[0]), float(band[1])),
        "stop_loss": float(stop_loss),
        "leverage": int(lev) if lev is not None else DEFAULT_LEV,
        "tp_count": int(tpn) if tpn is not None else None,
        "timeframe": tf,
    }


def _build_order_plan(
    coin: str,
    side: str,
    band: Tuple[float, float],
    leverage: int,
    stop_loss: float,
) -> Tuple[str, str, Dict[str, Any]]:
    """
    Decide entry price and size.
    Current logic:
      - px_entry: mid of band (safe, within signal’s bracket)
      - size: base size so that notional ~= NOTIONAL_USD
    """
    low, high = band
    px_entry = (low + high) / 2.0

    # notional in USD -> base size
    if px_entry <= 0:
        raise RuntimeError("Entry price invalid (<= 0).")
    base_sz = NOTIONAL_USD / px_entry

    # Ensure positive size
    base_sz = max(base_sz, 10 ** -6)

    info = _get_info()
    px_str, sz_str = _fmt_px_sz_for_perp(info, coin, px_entry, base_sz)

    brackets: Dict[str, Any] = {
        "px_entry": px_entry,
        "stop_loss": stop_loss,
        "tif": TIF,
        "notional_usd": NOTIONAL_USD,
    }
    return sz_str, px_str, brackets


# ---------------------------------------------------------------------------------
# Real placement (SDK) — guarded by DRY_RUN and config validation
# ---------------------------------------------------------------------------------
def _place_order_real(
    coin: str,
    asset_idx: int,
    is_buy: bool,
    px_str: str,
    sz_str: str,
    reduce_only: bool = False,
) -> Dict[str, Any]:
    """
    Place a single LIMIT order using the SDK's Exchange client.
    NOTE:
      - You MUST provide AGENT_PRIVATE_KEY in env for signing.
      - This function is minimal; adapt tif / cloid / grouping as needed.
    """
    if DRY_RUN:
        raise RuntimeError("DRY_RUN is enabled; refusing to place a real order.")

    if Exchange is None or Info is None:
        raise RuntimeError("hyperliquid-python-sdk not available; cannot place orders.")

    if not AGENT_PRIVATE_KEY:
        raise RuntimeError("HYPER_AGENT_PRIVATE_KEY env is required for real placement.")

    # Init clients
    if NETWORK.startswith("test"):
        info = Info(base_url="https://api.hyperliquid-testnet.xyz")
        ex = Exchange(AGENT_PRIVATE_KEY, base_url="https://api.hyperliquid-testnet.xyz")
    else:
        info = Info(base_url="https://api.hyperliquid.xyz")
        ex = Exchange(AGENT_PRIVATE_KEY, base_url="https://api.hyperliquid.xyz")

    # Build order payload (limit)
    order = {
        "a": asset_idx,            # asset index
        "b": bool(is_buy),         # isBuy
        "p": str(px_str),          # price
        "s": str(sz_str),          # size
        "r": bool(reduce_only),    # reduceOnly
        "t": {"limit": {"tif": TIF}},
    }

    action = {
        "type": "order",
        "orders": [order],
        "grouping": "na",
    }

    nonce = int(time.time() * 1000)

    # Exchange client hides signing; but some SDK versions accept direct payloads
    try:
        resp = ex.order(action, nonce=nonce, vaultAddress=VAULT_ADDRESS or None)  # type: ignore[attr-defined]
        return {"status": "ok", "response": resp}
    except Exception as e:
        raise RuntimeError(f"SDK order failed: {e}") from e


# ---------------------------------------------------------------------------------
# Public entry — called by execution.execute_signal
# ---------------------------------------------------------------------------------
def submit_signal(sig: Any) -> None:
    # Normalize input
    s = _extract_signal(sig)

    symbol = s["symbol"]
    if not _symbol_allowed(symbol):
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}")
        return

    coin = _symbol_to_coin(symbol)
    side = s["side"]
    band = s["band"]
    stop_loss = s["stop_loss"]
    lev = s["leverage"]

    print(f"[BROKER] {side} {symbol} band=({band[0]:.6f},{band[1]:.6f}) SL={stop_loss} lev={lev} TIF={TIF}")

    # Ensure we have a mark (not mandatory for current sizing, but useful logs)
    mark = _get_mark_price(coin)
    if mark is None:
        print(f"[PRICE] mark fetch failed for {coin}; proceeding with entry midpoint.")
    else:
        print(f"[PRICE] {coin} mark={mark}")

    # Plan size & price
    size_str, px_entry_str, brackets = _build_order_plan(
        coin=coin, side=side, band=band, leverage=lev, stop_loss=stop_loss
    )

    is_buy = side == "LONG"
    reduce_only = False

    # Resolve asset index (perps)
    info = _get_info()
    asset_idx = _asset_index(info, coin) if info else None
    if asset_idx is None:
        msg = f"Could not resolve asset index for {coin}; cannot place order."
        if DRY_RUN:
            print(f"[DRYRUN] {msg}")
            print(f"[DRYRUN] would place: side={'BUY' if is_buy else 'SELL'} coin={coin} a=? px={px_entry_str} sz={size_str} tif={TIF}")
            return
        raise RuntimeError(msg)

    # Log planned order
    print(f"[PLAN] side={'BUY' if is_buy else 'SELL'} coin={coin} a={asset_idx} px={px_entry_str} sz={size_str} tif={TIF} reduceOnly={reduce_only}")

    # Place or dry-run
    if DRY_RUN:
        print(f"[DRYRUN] submit LIMIT {('BUY' if is_buy else 'SELL')} {coin} a={asset_idx} px={px_entry_str} sz={size_str} tif={TIF}")
        return

    # Real placement
    resp = _place_order_real(
        coin=coin,
        asset_idx=asset_idx,
        is_buy=is_buy,
        px_str=px_entry_str,
        sz_str=size_str,
        reduce_only=reduce_only,
    )
    print(f"[BROKER] order response: {resp}")
