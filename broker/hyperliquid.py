# broker/hyperliquid.py
from __future__ import annotations

import os
import time
import logging
from typing import Dict, Optional, List, Any, Tuple

import requests

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

BASE_URL = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/")
NETWORK = os.getenv("HYPER_NETWORK", "mainnet").lower()
CHAIN_NAME = "Mainnet" if NETWORK == "mainnet" else "Testnet"

EVM_PRIVKEY = os.getenv("HYPER_EVM_PRIVKEY", "").strip()
EVM_CHAIN_ID = int(os.getenv("HYPER_EVM_CHAIN_ID", "999" if NETWORK == "mainnet" else "998"))

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ACCOUNT_MODE = os.getenv("ACCOUNT_MODE", "perp")
EXECUTION_MODE = os.getenv("XECUTION_MODE", "OTO").upper()
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "20"))
FIXED_QTY = os.getenv("HYPER_FIXED_QTY")
TP_WEIGHTS = [float(x) for x in os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20").split(",")]
ONLY_EXECUTE = {s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()}
ENTRY_TIMEOUT_MIN = int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))
POLL_OPEN_ORDERS_SEC = int(os.getenv("POLL_OPEN_ORDERS_SEC", "20"))
VAULT_ADDRESS = os.getenv("HYPER_VAULT_ADDRESS", "").strip()

# --- SDK (optional but recommended) ------------------------------------------
SDK_AVAILABLE = True
try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    try:
        from hyperliquid.utils import constants as HL_CONST  # not required
    except Exception:
        HL_CONST = None
except Exception as e:
    SDK_AVAILABLE = False
    LOG.warning("Hyperliquid SDK not available: %s", e)

_SDK_INFO: Optional["Info"] = None
_SDK_EX: Optional["Exchange"] = None


def _sdk_info() -> "Info":
    if not SDK_AVAILABLE:
        raise RuntimeError("Hyperliquid SDK not installed. Install `hyperliquid-python-sdk`.")
    global _SDK_INFO
    if _SDK_INFO is None:
        _SDK_INFO = Info(base_url=BASE_URL)
    return _SDK_INFO


def _sdk_exchange() -> "Exchange":
    if not SDK_AVAILABLE:
        raise RuntimeError("Hyperliquid SDK not installed. Install `hyperliquid-python-sdk`.")
    if not EVM_PRIVKEY:
        raise RuntimeError("HYPER_EVM_PRIVKEY not set. Needed to sign HL actions.")
    global _SDK_EX
    if _SDK_EX is None:
        _SDK_EX = Exchange(
            base_url=BASE_URL,
            chain=CHAIN_NAME,
            key=EVM_PRIVKEY,
            vault_address=VAULT_ADDRESS or None,
        )
    return _SDK_EX

# --- Meta cache --------------------------------------------------------------


class MetaCache:
    def __init__(self):
        self._meta = None
        self._asset_index: Dict[str, int] = {}
        self._sz_decimals: Dict[str, int] = {}
        self._last_refresh = 0

    def ensure(self):
        if not SDK_AVAILABLE:
            return
        now = time.time()
        if self._meta is None or now - self._last_refresh > 300:
            info = _sdk_info()
            self._meta = info.meta()
            self._last_refresh = now
            universe = self._meta.get("universe", [])
            self._asset_index.clear()
            self._sz_decimals.clear()
            for idx, entry in enumerate(universe):
                coin = (entry.get("name") or entry.get("spotName") or "").upper()
                if coin:
                    self._asset_index[coin] = idx
                    self._sz_decimals[coin] = int(entry.get("szDecimals", 0))

    def asset_index(self, coin: str) -> int:
        coin = coin.upper()
        if SDK_AVAILABLE:
            self.ensure()
            if coin in self._asset_index:
                return self._asset_index[coin]
        raise RuntimeError(f"Asset index for {coin} not found. Ensure SDK/meta available.")

    def sz_decimals(self, coin: str) -> int:
        coin = coin.upper()
        if SDK_AVAILABLE:
            self.ensure()
            if coin in self._sz_decimals:
                return self._sz_decimals[coin]
        return 3


META = MetaCache()

# --- Helpers -----------------------------------------------------------------


def _symbol_to_coin(symbol: str) -> str:
    s = symbol.upper().strip()
    return s.split("/")[0] if "/" in s else s


def _get_mark_price(coin: str) -> float:
    coin = coin.upper()
    if SDK_AVAILABLE:
        info = _sdk_info()
        try:
            d = info.active_asset_ctx(coin)
            ctx = d.get("ctx", {})
            mark = float(ctx.get("markPx"))
            if mark > 0:
                return mark
        except Exception as e:
            LOG.warning("active_asset_ctx failed for %s: %s", coin, e)
        try:
            mids = info.all_mids()
            mid = float(mids.get("mids", {}).get(coin))
            if mid > 0:
                return mid
        except Exception as e:
            LOG.warning("all_mids failed for %s: %s", coin, e)
    # Fallback: HTTP l2Book midpoint
    try:
        r = requests.post(
            f"{BASE_URL}/info",
            json={"type": "l2Book", "coin": coin, "nSigFigs": 5, "mantissa": None},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        levels = data.get("levels", [[], []])
        bids, asks = levels[0], levels[1]
        if bids and asks:
            best_bid = float(bids[0]["px"])
            best_ask = float(asks[0]["px"])
            return (best_bid + best_ask) / 2.0
    except Exception as e:
        LOG.error("HTTP l2Book fallback failed for %s: %s", coin, e)
    raise RuntimeError("Could not compute size from mark price; aborting.")


def quantize_size(coin: str, sz: float) -> str:
    sd = META.sz_decimals(coin)
    q = round(sz, sd)
    return f"{q:.{sd}f}"


def quantize_price(coin: str, px: float, is_perp: bool = True) -> str:
    sd = META.sz_decimals(coin)
    max_decimals = (6 if is_perp else 8) - sd
    max_decimals = max(max_decimals, 0)
    q = round(px, max_decimals)
    out = f"{q:.{max_decimals}f}"
    if "." in out:
        digits = out.replace(".", "").lstrip("0")
        if len(digits) > 5 and max_decimals > 0:
            cut = max(0, max_decimals - (len(digits) - 5))
            q = round(px, cut)
            out = f"{q:.{cut}f}"
    return out


def _read(sig: Any, *names, default=None):
    for n in names:
        if hasattr(sig, n):
            return getattr(sig, n)
        if isinstance(sig, dict) and n in sig:
            return sig[n]
    return default


def _maybe_tuple(v: Any) -> Optional[Tuple[float, float]]:
    try:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            lo, hi = v
            return float(lo), float(hi)
    except Exception:
        pass
    return None


def _extract_signal(sig: Any) -> Dict[str, Any]:
    side = (_read(sig, "side", "direction") or "").upper()
    symbol = _read(sig, "symbol", "ticker", "pair") or ""

    # Try a broad set of alias names for band low/high:
    low_aliases = (
        "band_low", "band_lo", "band_min",
        "entry_low", "entry_min",
        "low", "lo", "min", "lower",
        "entry_band_low", "entry_band_lo", "entry_band_min",
    )
    high_aliases = (
        "band_high", "band_hi", "band_max",
        "entry_high", "entry_max",
        "high", "hi", "max", "upper",
        "entry_band_high", "entry_band_hi", "entry_band_max",
    )

    band_low = _read(sig, *low_aliases)
    band_high = _read(sig, *high_aliases)

    # If still missing, auto-detect any tuple/list band field.
    if band_low is None or band_high is None:
        # Common tuple field names:
        tuple_names = ("band", "entry_band", "entry", "range", "price_band", "entry_range")
        for name in tuple_names:
            v = _read(sig, name)
            pair = _maybe_tuple(v)
            if pair:
                band_low, band_high = pair
                break
        # If still none, scan any attribute/dict key that contains 'band' or 'entry'
        if (band_low is None or band_high is None) and not isinstance(sig, dict):
            try:
                for k, v in vars(sig).items():
                    if isinstance(k, str) and any(s in k.lower() for s in ("band", "entry")):
                        pair = _maybe_tuple(v)
                        if pair:
                            band_low, band_high = pair
                            break
            except Exception:
                pass
        if (band_low is None or band_high is None) and isinstance(sig, dict):
            for k, v in sig.items():
                if isinstance(k, str) and any(s in k.lower() for s in ("band", "entry")):
                    pair = _maybe_tuple(v)
                    if pair:
                        band_low, band_high = pair
                        break

    stop_loss = _read(sig, "stop_loss", "sl", "stop", "stopPrice")
    tp_count = _read(sig, "tp_count", "tpn", "tpN", "take_profit_count", default=1)
    leverage = _read(sig, "leverage", "lev", "x", default=None)
    timeframe = _read(sig, "timeframe", "tf", default="")

    if not side or not symbol:
        raise ValueError(f"Signal missing side/symbol: {sig}")

    if band_low is None or band_high is None:
        tried = ", ".join(low_aliases) + " / " + ", ".join(high_aliases)
        raise ValueError(f"Signal missing band_low/band_high. Tried aliases: {tried}")

    if stop_loss is None:
        raise ValueError("Signal missing stop_loss/SL.")

    try:
        tp_count = int(tp_count)
    except Exception:
        tp_count = 1

    try:
        leverage = int(leverage) if leverage is not None else None
    except Exception:
        leverage = None

    return dict(
        side=side,
        symbol=str(symbol),
        band_low=float(band_low),
        band_high=float(band_high),
        stop_loss=float(stop_loss),
        tp_count=tp_count,
        leverage=leverage,
        timeframe=str(timeframe),
    )

# --- Public API --------------------------------------------------------------


def submit_signal(sig: Any) -> None:
    s = _extract_signal(sig)

    if ONLY_EXECUTE:
        if s["symbol"].upper() not in ONLY_EXECUTE and _symbol_to_coin(s["symbol"]) not in ONLY_EXECUTE:
            LOG.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", s["symbol"])
            return

    coin = _symbol_to_coin(s["symbol"])

    LOG.info(
        "[BROKER] %s %s band=(%.6f, %.6f) SL=%.6f TPn=%d lev=%s TF=%s",
        s["side"], s["symbol"], s["band_low"], s["band_high"], s["stop_loss"],
        s["tp_count"], str(s["leverage"]) if s["leverage"] is not None else "—", s["timeframe"]
    )

    size_str, px_entry_str, brackets = _build_order_plan(
        side=s["side"], coin=coin,
        band_low=s["band_low"], band_high=s["band_high"],
        stop_loss=s["stop_loss"], tp_count=s["tp_count"]
    )

    if DRY_RUN:
        LOG.info(
            "[DRY_RUN] Would place %s %s size=%s @ %s with %d TP(s) and SL=%s",
            s["side"], coin, size_str, px_entry_str, len(brackets['tps']), brackets['sl']["px"]
        )
        return

    payload = {
        "coin": coin,
        "asset": META.asset_index(coin),
        "side": s["side"],
        "px_entry": px_entry_str,
        "size": size_str,
        "tp_list": brackets["tps"],
        "sl": brackets["sl"],
        "leverage": s["leverage"],
        "grouping": "positionTpsl",
        "tif": "Ioc",
    }
    _place_order_real(payload)


def _build_order_plan(*, side: str, coin: str, band_low: float, band_high: float, stop_loss: float, tp_count: int):
    side_up = side.upper()
    px_entry = band_low if side_up == "SHORT" else band_high

    mark = _get_mark_price(coin)

    if FIXED_QTY:
        base_qty = float(FIXED_QTY)
    else:
        notional = max(TRADE_SIZE_USD, 10.0)
        base_qty = notional / max(mark, 1e-9)

    size_str = quantize_size(coin, base_qty)
    px_entry_str = quantize_price(coin, px_entry, is_perp=True)

    tp_n = max(1, min(tp_count or 1, len(TP_WEIGHTS)))
    weights = TP_WEIGHTS[:tp_n]
    wsum = sum(weights)
    weights = [w / wsum for w in weights]

    tps: List[Dict[str, str]] = []
    step = 0.0025
    for i, w in enumerate(weights, start=1):
        tp_px = px_entry * (1 - step * i) if side_up == "SHORT" else px_entry * (1 + step * i)
        tp_px_str = quantize_price(coin, tp_px, is_perp=True)
        child_sz = float(size_str) * w
        child_sz_str = quantize_size(coin, child_sz)
        tps.append({"px": tp_px_str, "sz": child_sz_str})

    sl_px_str = quantize_price(coin, float(stop_loss), is_perp=True)
    return size_str, px_entry_str, {"tps": tps, "sl": {"px": sl_px_str}}


def _place_order_real(plan: Dict) -> None:
    coin = plan["coin"]
    asset = plan["asset"]
    is_buy = plan["side"].upper() == "LONG"
    px_entry = plan["px_entry"]
    size = plan["size"]
    tif = plan.get("tif", "Ioc")
    tps = plan.get("tp_list", [])
    sl = plan.get("sl")
    leverage = plan.get("leverage")
    grouping = plan.get("grouping", "positionTpsl")

    if not SDK_AVAILABLE:
        raise NotImplementedError(
            "Hyperliquid SDK is required for live placement. "
            "Install `hyperliquid-python-sdk` and set HYPER_EVM_PRIVKEY."
        )

    ex = _sdk_exchange()

    if leverage:
        try:
            ex.update_leverage(asset=asset, is_cross=True, leverage=int(leverage))
            LOG.info("[HL] leverage updated to %sx (cross) on %s", leverage, coin)
        except Exception as e:
            LOG.warning("[HL] update_leverage failed on %s: %s", coin, e)

    orders = []
    orders.append({
        "a": asset,
        "b": is_buy,
        "p": str(px_entry),
        "s": str(size),
        "r": False,
        "t": {"limit": {"tif": tif}},
    })

    for tp in tps:
        orders.append({
            "a": asset,
            "b": not is_buy,
            "p": "0",
            "s": tp["sz"],
            "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": tp["px"], "tpsl": "tp"}},
        })

    if sl:
        orders.append({
            "a": asset,
            "b": not is_buy,
            "p": "0",
            "s": str(size),
            "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": sl["px"], "tpsl": "sl"}},
        })

    try:
        res = ex.place_orders(
            orders=orders,
            grouping=grouping,
            vault_address=VAULT_ADDRESS or None,
        )
        LOG.info("[HL] order response: %s", str(res))
    except Exception as e:
        msg = str(e)
        if "MinTradeNtl" in msg:
            LOG.error("Rejected: below minimum $10 notional.")
        elif "BadTriggerPx" in msg:
            LOG.error("Rejected: invalid TP/SL trigger price.")
        elif "PerpMargin" in msg:
            LOG.error("Rejected: insufficient margin.")
        elif "Tick" in msg:
            LOG.error("Rejected: price not divisible by tick size.")
        else:
            LOG.error("[HL] placement failed: %s", msg)
        raise
