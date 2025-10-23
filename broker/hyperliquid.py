# broker/hyperliquid.py
from __future__ import annotations

import os
import logging as log
from typing import Any, Dict, List, Optional, Tuple

# SDK imports (prefer these modules; older "from hyperliquid import Exchange" fails)
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

# Use SDK Wallet if present
try:
    from hyperliquid.utils.wallet import Wallet  # type: ignore
    _HAS_SDK_WALLET = True
except Exception:
    _HAS_SDK_WALLET = False

# Local signing fallback
from eth_account import Account
from eth_account.messages import encode_defunct, SignableMessage

# ---------- Environment ----------
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY") or ""
ALLOWED = [s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()]
DEFAULT_TIF = (os.getenv("HYPER_DEFAULT_TIF") or "PostOnly").strip()
BASE_USD = float(os.getenv("HYPER_BASE_USD") or "50.0")  # default sizing if you don't pass one

if not PRIVKEY:
    # We'll raise a clear error later if you try to submit without a key
    log.info("[BROKER] No HYPER_PRIVATE_KEY present yet.")

log.info("[BROKER] hyperliquid.py loaded")

# ---------- Helpers ----------

def _split_symbol(symbol: str) -> str:
    """Convert 'BTC/USD' -> 'BTC' (coin name used by SDK Info.name_to_asset)."""
    if not symbol:
        return symbol
    s = symbol.upper().strip()
    if "/" in s:
        return s.split("/")[0]
    return s

def _mk_agent_from_privkey(priv: str) -> Any:
    """
    Prefer the SDK's Wallet; otherwise fall back to minimal eth-account agent
    that matches the interface the SDK expects.
    """
    if _HAS_SDK_WALLET:
        return Wallet(priv)
    return _EthAccountAgent(priv)


class _EthAccountAgent:
    """
    Minimal signer compatible with hyperliquid’s Exchange expectations.
    Handles str, bytes, and SignableMessage correctly.
    """
    def __init__(self, priv: str):
        self._acct = Account.from_key(priv)

    def sign_message(self, msg: Any) -> Dict[str, str]:
        # SDK sometimes passes an already-built SignableMessage
        if isinstance(msg, SignableMessage):
            signed = self._acct.sign_message(msg)
        elif isinstance(msg, (bytes, bytearray)):
            signed = self._acct.sign_message(encode_defunct(text=msg.decode("utf-8")))
        elif isinstance(msg, str):
            signed = self._acct.sign_message(encode_defunct(text=msg))
        else:
            # Fallback: stringify
            signed = self._acct.sign_message(encode_defunct(text=str(msg)))
        return {"signature": signed.signature.hex()}

    # If the SDK ever calls this we can implement EIP-712 later
    def sign_typed_data(self, typed_data: dict) -> dict:
        raise NotImplementedError("EIP-712 signing not implemented in fallback agent")

def _mk_clients() -> Tuple[Exchange, Info]:
    if not PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")
    agent = _mk_agent_from_privkey(PRIVKEY)
    ex = Exchange(agent=agent)  # wallet auth path; compat across SDK versions
    info = Info()
    log.info(f"[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY={PRIVKEY[:6]}…{PRIVKEY[-4:]} "
             f"(agent={'Wallet' if _HAS_SDK_WALLET else '_EthAccountAgent'})")
    return ex, info

def _allowed(symbol: str) -> bool:
    if not ALLOWED:
        return True
    return symbol.upper() in ALLOWED

def _mid(a: float, b: float) -> float:
    return (float(a) + float(b)) / 2.0

def _trim_size_for_sdk(sz: float, max_iters: int = 6) -> float:
    """
    Reduce precision to avoid 'float_to_wire causes rounding'.
    Tries 6,5,4,... decimals. Returns the first that likely passes.
    """
    s = sz
    for decimals in range(6, -1, -1):
        s_rounded = float(f"{s:.{decimals}f}")
        if s_rounded > 0:
            return s_rounded
    return max(s, 0.0)

def _post_only_order_dict(coin: str, is_buy: bool, sz: float, px: float, tif: str) -> dict:
    """
    Build an order dict that the SDK's bulk_orders -> signing.order_request_to_order_wire accepts.
    Avoids importing OrderType enums (which vary by SDK version).
    """
    tif_norm = tif.strip().lower()
    if tif_norm in ("postonly", "post_only", "post-only"):
        order_type = {"limit": {"tif": "PostOnly"}}
    elif tif_norm in ("gtc", "goodtillcancel", "good_till_cancel"):
        order_type = {"limit": {"tif": "Gtc"}}
    elif tif_norm in ("ioc", "immediateorcancel", "immediate_or_cancel"):
        order_type = {"limit": {"tif": "Ioc"}}
    else:
        order_type = {"limit": {"tif": "PostOnly"}}

    return {
        "coin": coin,          # e.g., "BTC"
        "is_buy": bool(is_buy),
        "sz": float(sz),
        "limit_px": float(px),
        "order_type": order_type,
        "reduce_only": False,
    }

def _try_bulk_with_rounding(ex: Exchange, order: dict) -> Any:
    """
    Calls bulk_orders; if we hit float_to_wire rounding on size, trim and retry; if on price,
    nudge price by a minimal tick (1e-6) and retry. Also tolerates the SDK passing different
    signable message types to our agent.
    """
    last_err: Optional[Exception] = None
    # Up to a handful of attempts adjusting size/price minimal amounts
    sz = float(order["sz"])
    px = float(order["limit_px"])
    for _ in range(8):
        order["sz"] = _trim_size_for_sdk(sz)
        order["limit_px"] = float(f"{px:.8f}")  # cap to 8 dp to help the SDK round trip
        try:
            return ex.bulk_orders([order])
        except Exception as e:
            msg = str(e)
            last_err = e
            if "float_to_wire causes rounding" in msg:
                # If it's on sz: shrink a bit more; if on p: nudge price by tiny amount away from midpoint
                if "'s':" in msg or " 's'" in msg:
                    sz = max(sz - sz * 0.1, sz * 0.9)
                elif "'p':" in msg or " 'p'" in msg:
                    # Nudge by one minimal tick
                    px = px * 0.999999
                else:
                    sz = _trim_size_for_sdk(sz)
            elif "SignableMessage" in msg or "encode" in msg:
                # Our agent handles this; continue to let it retry in case SDK re-wraps message.
                pass
            else:
                # Any other error -> break and bubble up
                break
    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# ---------- Public entry point ----------

def submit_signal(sig: Any) -> None:
    """
    Accepts an ExecSignal-like object with attributes:
      - side: 'LONG' or 'SHORT'
      - symbol: like 'BTC/USD'
      - band_low, band_high (floats)
      - stop_loss (float)  [optional for this simple entry, not used here]
      - leverage (float)   [optional; not used for sizing here]
      - tif (optional str) [PostOnly/Gtc/Ioc]
      - base_usd (optional float) notional to size from; default BASE_USD
    """
    symbol = getattr(sig, "symbol")
    side = getattr(sig, "side")
    if not symbol or not side:
        raise ValueError("Signal missing side and/or symbol.")

    if not _allowed(symbol):
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}")
        return

    band_low = getattr(sig, "band_low", None)
    band_high = getattr(sig, "band_high", None)
    if band_low is None or band_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    tif = getattr(sig, "tif", None) or DEFAULT_TIF

    ex, info = _mk_clients()

    coin = _split_symbol(symbol)
    is_buy = str(side).upper() == "LONG"

    # Mid price entry
    px = _mid(float(band_low), float(band_high))

    # Basic sizing: use BASE_USD notional unless a base_usd override is carried in sig.
    notional = float(getattr(sig, "base_usd", BASE_USD))
    # Guard for zero price
    px_safe = max(float(px), 1e-9)
    raw_sz = notional / px_safe

    # Trim to avoid wire rounding issues
    sz = _trim_size_for_sdk(raw_sz)

    order = _post_only_order_dict(coin=coin, is_buy=is_buy, sz=sz, px=px, tif=tif)

    log.info(f"[BROKER] {side} {symbol} band=({float(band_low):.6f},{float(band_high):.6f}) "
             f"SL={getattr(sig, 'stop_loss', None)} lev={getattr(sig, 'leverage', None)} TIF={tif}")
    log.info(f"[BROKER] PLAN side={'BUY' if is_buy else 'SELL'} coin={coin} "
             f"px={float(px):.8f} sz={sz} tif={tif} reduceOnly=False")

    # Place the order, coping with SDK float/signing quirks
    try:
        resp = _try_bulk_with_rounding(ex, order)
    except Exception as e:
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e

    log.info(f"[BROKER] order response: {resp}")
