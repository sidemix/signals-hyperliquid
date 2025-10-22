# broker/hyperliquid.py
# Drop-in Hyperliquid broker for your VIP Signals worker.
# - Uses Hyperliquid official Python SDK when available.
# - Caches meta, resolves asset indices, handles size/price quantization.
# - Places entry + TP/SL with grouping, updates leverage when provided.
# - Graceful DRY_RUN and detailed error messages.

from __future__ import annotations

import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import requests

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

# --- Environment -------------------------------------------------------------

BASE_URL = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/")
NETWORK = os.getenv("HYPER_NETWORK", "mainnet").lower()  # "mainnet" or "testnet"
CHAIN_NAME = "Mainnet" if NETWORK == "mainnet" else "Testnet"

# If you use HL API wallets (recommended), you can sign with a private key.
# These two are what your screenshots show you already have:
EVM_PRIVKEY = os.getenv("HYPER_EVM_PRIVKEY", "").strip()
EVM_CHAIN_ID = int(os.getenv("HYPER_EVM_CHAIN_ID", "999" if NETWORK == "mainnet" else "998"))

# Optional “API key/secret” placeholders – HL doesn’t use CEX-style HMAC keys.
# We keep them only because your env has them; they’re not used for signing.
HL_API_KEY = os.getenv("HYPER_API_KEY", "").strip()
HL_API_SECRET = os.getenv("HYPER_API_SECRET", "").strip()

# App-level controls you already set
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ACCOUNT_MODE = os.getenv("ACCOUNT_MODE", "perp")  # "perp" | "spot" (we assume perp)
EXECUTION_MODE = os.getenv("XECUTION_MODE", "OTO").upper()  # OTO means entry with TP/SL
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "20"))  # notional in USD
FIXED_QTY = os.getenv("HYPER_FIXED_QTY")  # override qty in base if provided
TP_WEIGHTS = [float(x) for x in os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20").split(",")]

ONLY_EXECUTE = {s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()}

ENTRY_TIMEOUT_MIN = int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))
POLL_OPEN_ORDERS_SEC = int(os.getenv("POLL_OPEN_ORDERS_SEC", "20"))

# Subaccount/vault trading (optional)
VAULT_ADDRESS = os.getenv("HYPER_VAULT_ADDRESS", "").strip()  # 0x... if you trade from a subaccount

# -----------------------------------------------------------------------------
# Optional: import the official HL SDK if present
# -----------------------------------------------------------------------------

SDK_AVAILABLE = True
try:
    # pip install hyperliquid-python-sdk
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    # Some sdk versions expose constants/helpers under utils
    try:
        from hyperliquid.utils import constants as HL_CONST
    except Exception:
        HL_CONST = None
except Exception as e:
    SDK_AVAILABLE = False
    LOG.warning("Hyperliquid Python SDK not available: %s", e)

# -----------------------------------------------------------------------------
# Simple signal model from your parser/execution layer
# -----------------------------------------------------------------------------

@dataclass
class ParsedSignal:
    side: str              # "LONG" | "SHORT"
    symbol: str            # "ETH/USD"
    band_low: float        # entry band lo
    band_high: float       # entry band hi
    stop_loss: float       # SL price
    tp_count: int          # number of take-profits requested
    leverage: float        # requested leverage (e.g. 20)
    timeframe: str         # "5m" etc.

# -----------------------------------------------------------------------------
# Asset/meta cache + helpers
# -----------------------------------------------------------------------------

class MetaCache:
    def __init__(self):
        self._meta = None
        self._asset_index: Dict[str, int] = {}
        self._sz_decimals: Dict[str, int] = {}
        self._last_refresh = 0

    def ensure(self):
        if SDK_AVAILABLE:
            now = time.time()
            if self._meta is None or now - self._last_refresh > 300:
                info = _sdk_info()
                self._meta = info.meta()
                self._last_refresh = now
                # Build quick maps for perps
                universe = self._meta.get("universe", [])  # perps
                for idx, entry in enumerate(universe):
                    coin = entry.get("name") or entry.get("spotName") or ""
                    if coin:
                        self._asset_index[coin.upper()] = idx
                        self._sz_decimals[coin.upper()] = int(entry.get("szDecimals", 0))

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
        # Fallback conservative default
        return 3


META = MetaCache()

# -----------------------------------------------------------------------------
# Price/size quantization to match HL rules
# -----------------------------------------------------------------------------

def quantize_size(coin: str, sz: float) -> str:
    sd = META.sz_decimals(coin)
    q = round(sz, sd)
    fmt = f"{{:.{sd}f}}"
    return fmt.format(q)

def quantize_price(coin: str, px: float, is_perp: bool = True) -> str:
    # HL: <= 5 significant figures; and no more than (6 - szDecimals) decimals for perps
    sd = META.sz_decimals(coin)
    max_decimals = (6 if is_perp else 8) - sd
    max_decimals = max(max_decimals, 0)
    q = round(px, max_decimals)
    fmt = f"{{:.{max_decimals}f}}"
    out = fmt.format(q)
    # ensure no excess sig figs for non-integers
    if "." in out:
        digits = out.replace(".", "").lstrip("0")
        if len(digits) > 5 and max_decimals > 0:
            # reduce decimals to respect 5 significant figures
            cut = max(0, max_decimals - (len(digits) - 5))
            q = round(px, cut)
            out = f"{q:.{cut}f}"
    return out

# -----------------------------------------------------------------------------
# Mark price resolver
# -----------------------------------------------------------------------------

def _get_mark_price(coin: str) -> float:
    coin = coin.upper()
    if SDK_AVAILABLE:
        info = _sdk_info()
        try:
            d = info.active_asset_ctx(coin)
            # For perps, ctx contains markPx
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
    # Final fallback: l2book via HTTP info
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
        LOG.error("HTTP l2Book mark price fallback failed for %s: %s", coin, e)
    raise RuntimeError("Could not compute size from mark price; aborting.")

# -----------------------------------------------------------------------------
# SDK bootstrap
# -----------------------------------------------------------------------------

_SDK_INFO: Optional[Info] = None
_SDK_EX: Optional[Exchange] = None

def _sdk_info() -> Info:
    global _SDK_INFO
    if _SDK_INFO is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("Hyperliquid SDK not installed. Install `hyperliquid-python-sdk`.")
        _SDK_INFO = Info(base_url=BASE_URL)
    return _SDK_INFO

def _sdk_exchange() -> Exchange:
    global _SDK_EX
    if _SDK_EX is None:
        if not SDK_AVAILABLE:
            raise RuntimeError("Hyperliquid SDK not installed. Install `hyperliquid-python-sdk`.")
        if not EVM_PRIVKEY:
            raise RuntimeError("HYPER_EVM_PRIVKEY not set. Needed to sign HL actions.")
        # Exchange signer based on EVM private key; pass chain and vault if any
        _SDK_EX = Exchange(
            base_url=BASE_URL,
            chain=CHAIN_NAME,      # "Mainnet" or "Testnet"
            account=None,          # let SDK derive from privkey
            key=EVM_PRIVKEY,       # private key string "0x..."
            vault_address=VAULT_ADDRESS or None,
        )
    return _SDK_EX

# -----------------------------------------------------------------------------
# Public entry-point used by execution.py
# -----------------------------------------------------------------------------

def submit_signal(sig: ParsedSignal) -> None:
    """
    Executes a signal into Hyperliquid.
      - side: LONG/SHORT
      - symbol: e.g., "ETH/USD"  -> coin "ETH"
      - band_low/band_high: preferred entry band
      - stop_loss: absolute price
      - tp_count: number of TPs requested (we'll cap to len(TP_WEIGHTS))
      - leverage: int/float
    """
    # Filter by allow-list if set
    if ONLY_EXECUTE:
        if sig.symbol.upper() not in ONLY_EXECUTE and _symbol_to_coin(sig.symbol) not in ONLY_EXECUTE:
            LOG.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", sig.symbol)
            return

    coin = _symbol_to_coin(sig.symbol)
    LOG.info("[BROKER] %s %s band=(%.6f,%.6f) SL=%.6f TPn=%d lev=%.1f TF=%s",
             sig.side, sig.symbol, sig.band_low, sig.band_high, sig.stop_loss,
             sig.tp_count, sig.leverage, sig.timeframe)

    # Compute size
    size_str, px_entry_str, brackets = _build_order_plan(sig, coin)

    # DRY RUN
    if DRY_RUN:
        LOG.info("[DRY_RUN] Would place %s %s size=%s @ %s with %d TP(s) and SL=%s",
                 sig.side, coin, size_str, px_entry_str, len(brackets["tps"]), brackets["sl"])
        return

    # Live
    payload = {
        "coin": coin,
        "asset": META.asset_index(coin),
        "side": sig.side.upper(),
        "px_entry": px_entry_str,
        "size": size_str,
        "tp_list": brackets["tps"],  # list of {"px": str, "sz": str}
        "sl": brackets["sl"],        # {"px": str}
        "leverage": int(sig.leverage) if sig.leverage else None,
        "grouping": "positionTpsl",
        "tif": "Ioc",                # IOC entry by default
    }
    _place_order_real(payload)

# -----------------------------------------------------------------------------
# Planning helpers
# -----------------------------------------------------------------------------

def _symbol_to_coin(symbol: str) -> str:
    # "ETH/USD" -> "ETH"
    s = symbol.upper().strip()
    if "/" in s:
        return s.split("/")[0]
    return s

def _build_order_plan(sig: ParsedSignal, coin: str):
    # Determine entry price (simple heuristic: for SHORT choose band_low; for LONG choose band_high)
    if sig.side.upper() == "SHORT":
        px_entry = sig.band_low
    else:
        px_entry = sig.band_high

    mark = _get_mark_price(coin)

    # Determine size
    if FIXED_QTY:
        base_qty = float(FIXED_QTY)
    else:
        # sz = notional / mark
        notional = max(TRADE_SIZE_USD, 10.0)  # respect HL min trade notional ($10)
        base_qty = notional / max(mark, 1e-9)

    size_str = quantize_size(coin, base_qty)
    px_entry_str = quantize_price(coin, px_entry, is_perp=True)

    # Build TPs: spread weights across size
    tp_n = max(1, min(sig.tp_count or 1, len(TP_WEIGHTS)))
    weights = TP_WEIGHTS[:tp_n]
    # Normalize weights to 1.0 (safety)
    wsum = sum(weights)
    weights = [w / wsum for w in weights]

    tps: List[Dict[str, str]] = []
    # simple 0.25% steps away from entry if band lacks explicit TPs
    # you can replace with your own TP ladder logic
    step = 0.0025
    for i, w in enumerate(weights, start=1):
        tp_px = px_entry * (1 - step * i) if sig.side.upper() == "SHORT" else px_entry * (1 + step * i)
        tp_px_str = quantize_price(coin, tp_px, is_perp=True)
        child_sz = float(size_str) * w
        child_sz_str = quantize_size(coin, child_sz)
        tps.append({"px": tp_px_str, "sz": child_sz_str})

    sl_px_str = quantize_price(coin, float(sig.stop_loss), is_perp=True)

    return size_str, px_entry_str, {"tps": tps, "sl": {"px": sl_px_str}}

# -----------------------------------------------------------------------------
# Real placement
# -----------------------------------------------------------------------------

def _place_order_real(plan: Dict) -> None:
    """Use HL SDK to submit:
         - optional leverage update
         - entry (IOC limit at provided px)
         - attach TP/SL triggers with grouping=positionTpsl
    """
    coin = plan["coin"]
    asset = plan["asset"]
    is_buy = plan["side"] == "LONG"
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
            "Install `hyperliquid-python-sdk` and provide HYPER_EVM_PRIVKEY."
        )

    ex = _sdk_exchange()

    # 1) Update leverage if requested
    if leverage:
        try:
            ex.update_leverage(asset=asset, is_cross=True, leverage=int(leverage))
            LOG.info("[HL] leverage updated to %sx (cross) on %s", leverage, coin)
        except Exception as e:
            LOG.warning("[HL] update_leverage failed on %s: %s", coin, e)

    # 2) Build orders
    orders = []

    # Entry (limit IOC as default “marketable” intent)
    orders.append({
        "a": asset,
        "b": is_buy,
        "p": str(px_entry),
        "s": str(size),
        "r": False,
        "t": {"limit": {"tif": tif}},
    })

    # Add TP triggers
    for tp in tps:
        orders.append({
            "a": asset,
            "b": not is_buy,  # opposite side to close
            "p": "0",         # ignored for trigger market=True
            "s": tp["sz"],
            "r": True,        # reduce-only
            "t": {"trigger": {"isMarket": True, "triggerPx": tp["px"], "tpsl": "tp"}},
        })

    # Add SL trigger
    if sl:
        orders.append({
            "a": asset,
            "b": not is_buy,
            "p": "0",
            "s": str(size),
            "r": True,
            "t": {"trigger": {"isMarket": True, "triggerPx": sl["px"], "tpsl": "sl"}},
        })

    # 3) Send one batched action with grouping
    try:
        res = ex.place_orders(
            orders=orders,
            grouping=grouping,       # "positionTpsl" recommended
            vault_address=VAULT_ADDRESS or None,
            # The SDK handles nonce/signature. If you need expiresAfter: pass expires_after_ms=...
        )
        LOG.info("[HL] order response: %s", str(res))
    except Exception as e:
        # Try to decode common server errors if possible
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

# -----------------------------------------------------------------------------
# Human-friendly execution entry for your logs
# -----------------------------------------------------------------------------

def preview_payload(sig: ParsedSignal) -> dict:
    """Utility for logging/tests."""
    coin = _symbol_to_coin(sig.symbol)
    size_str, px_entry_str, brackets = _build_order_plan(sig, coin)
    return {
        "coin": coin,
        "asset": META.asset_index(coin) if SDK_AVAILABLE else None,
        "side": sig.side.upper(),
        "px_entry": px_entry_str,
        "size": size_str,
        "leverage": sig.leverage,
        "tp_list": brackets["tps"],
        "sl": brackets["sl"],
        "grouping": "positionTpsl",
        "tif": "Ioc",
    }
