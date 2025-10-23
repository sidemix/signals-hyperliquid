# broker/hyperliquid.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# --- Import Exchange/Info (handle SDK variants) -----------------------------
try:
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.info import Info          # type: ignore
except Exception:
    from hyperliquid import Exchange, Info     # type: ignore  # noqa: F401

# Try to import an official Wallet from any known SDK location
_WALLET_CTOR = None
for _path in (
    "hyperliquid.wallet",
    "hyperliquid.utils.wallet",
    "hyperliquid.agent",
):
    try:
        mod = __import__(_path, fromlist=["Wallet"])
        if hasattr(mod, "Wallet"):
            _WALLET_CTOR = getattr(mod, "Wallet")
            break
    except Exception:
        pass

# --- ENV --------------------------------------------------------------------
ONLY = {s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip()}
DEFAULT_TIF = (os.getenv("HYPER_TIF", "PostOnly") or "PostOnly").strip()
PRIVKEY = os.getenv("HYPER_PRIVATE_KEY")  # REQUIRED

# --- Small helpers ----------------------------------------------------------
def _round8(x: float) -> float:
    return float(f"{x:.8f}")

def _normalize_resp(resp: Any) -> Any:
    if isinstance(resp, (bytes, bytearray)):
        try:
            return json.loads(resp.decode())
        except Exception:
            return resp
    return resp

def _order_type_from_tif(tif: str) -> Dict[str, Any]:
    t = (tif or "").strip().lower()
    if t == "postonly":
        return {"limit": {"tif": "Alo"}}
    if t in ("gtc", "ioc", "alo"):
        cap = {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}[t]
        return {"limit": {"tif": cap}}
    return {"limit": {"tif": "Gtc"}}

def _legacy_post_only() -> Dict[str, Any]:
    return {"postOnly": {}}

def _should_skip(symbol: str) -> bool:
    return bool(ONLY) and symbol.upper() not in ONLY

# --- ExecSignal (what execution.py passes to us) ----------------------------
@dataclass
class ExecSignal:
    side: str                # LONG/SHORT
    symbol: str              # e.g. BTC/USD
    entry_low: float
    entry_high: float
    stop_loss: Optional[float] = None
    leverage: Optional[float] = None
    tif: Optional[str] = None

# --- Shim wallet (if SDK doesn't give us one) -------------------------------
class _ShimWallet:
    """
    Wallet adapter backed by eth_account that exposes .address and .sign_message.
    Works with SDK flows that call wallet.sign_message(SignableMessage).
    """
    def __init__(self, privkey_hex: str):
        from eth_account import Account
        self._acct = Account.from_key(privkey_hex)
        self.address = self._acct.address

    def sign_message(self, signable):
        # The SDK usually hands us eth_account.messages.SignableMessage
        try:
            from eth_account.messages import SignableMessage
            if isinstance(signable, SignableMessage):
                sig = self._acct.sign_message(signable).signature.hex()
                return sig
        except Exception:
            pass

        # Fallbacks: hex string or bytes
        from eth_account.messages import encode_defunct
        if isinstance(signable, (bytes, bytearray)):
            msg = encode_defunct(signable.decode(errors="ignore"))
        elif isinstance(signable, str):
            if signable.startswith("0x"):
                try:
                    msg = encode_defunct(bytes.fromhex(signable[2:]).decode(errors="ignore"))
                except Exception:
                    msg = encode_defunct(signable)
            else:
                msg = encode_defunct(signable)
        else:
            # Last resort: JSON-ish representation
            try:
                msg = encode_defunct(json.dumps(signable, separators=(",", ":")))
            except Exception:
                msg = encode_defunct(str(signable))

        return self._acct.sign_message(msg).signature.hex()

# --- Build Exchange/Info with robust wallet handling ------------------------
def _mk_clients() -> Tuple[Any, Any]:
    if not PRIVKEY:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY.")

    # 1) Official Wallet class if the SDK provides it
    if _WALLET_CTOR is not None:
        try:
            w = _WALLET_CTOR(PRIVKEY)  # type: ignore
            ex = Exchange(wallet=w)     # type: ignore
            info = Info()
            return ex, info
        except Exception as e:  # noqa: BLE001
            last_err = e
    else:
        last_err = None

    # 2) Some builds accept a dict-style agent
    try:
        ex = Exchange(wallet={"privateKey": PRIVKEY})  # type: ignore
        info = Info()
        return ex, info
    except Exception as e:  # noqa: BLE001
        last_err = e

    # 3) Some builds accept the raw hex string
    try:
        ex = Exchange(wallet=PRIVKEY)  # type: ignore
        info = Info()
        return ex, info
    except Exception as e:  # noqa: BLE001
        last_err = e

    # 4) Shim wallet that exposes sign_message
    try:
        w = _ShimWallet(PRIVKEY)
        ex = Exchange(wallet=w)  # type: ignore
        info = Info()
        return ex, info
    except Exception as e:  # noqa: BLE001
        last_err = e

    raise RuntimeError(f"Could not construct Exchange with any wallet style: {last_err}")

# --- Bulk submit with order-type + rounding fallbacks -----------------------
def _try_bulk_with_rounding(ex: Any, order: Dict[str, Any]) -> Any:
    order = dict(order)
    order["sz"] = float(order["sz"])
    order["limit_px"] = float(order["limit_px"])

    def _bulk(o: Dict[str, Any]) -> Any:
        return _normalize_resp(ex.bulk_orders([o]))

    # 1) Preferred modern Alo form
    try:
        return _bulk(order)
    except Exception as e1:  # noqa: BLE001
        msg1 = str(e1)
        # 2) Legacy fallback when server dislikes modern type
        if "Invalid order type" in msg1 or "'postOnly'" in msg1:
            legacy = dict(order)
            legacy["order_type"] = _legacy_post_only()
            try:
                return _bulk(legacy)
            except Exception as e2:  # noqa: BLE001
                last_err = e2
        else:
            last_err = e1

    # 3) Size nudge to avoid float_to_wire rounding guard
    step = 1e-8
    for _ in range(6):
        new_sz = max(0.0, float(order["sz"]) - step)
        if new_sz <= 0.0:
            break
        order["sz"] = _round8(new_sz)
        try:
            return _bulk(order)
        except Exception as e3:  # noqa: BLE001
            last_err = e3

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# --- Public API used by execution.py ----------------------------------------
def submit_signal(sig: ExecSignal) -> None:
    symbol = sig.symbol.upper()
    if _should_skip(symbol):
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    if sig.entry_low is None or sig.entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    ex, _info = _mk_clients()
    log.info("[BROKER] hyperliquid.py loaded")

    coin = symbol.split("/")[0]
    mid_px = (float(sig.entry_low) + float(sig.entry_high)) / 2.0
    px = _round8(mid_px)

    notional_usd = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
    sz = _round8(max(1e-8, notional_usd / max(px, 1e-8)))

    is_buy = sig.side.upper() == "LONG"
    tif = (sig.tif or DEFAULT_TIF or "PostOnly").strip()
    order_type = _order_type_from_tif(tif)

    order: Dict[str, Any] = {
        "coin": coin,
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": px,
        "order_type": order_type,
        "reduce_only": False,
    }

    log.info("[BROKER] BUY %s band=(%f,%f) SL=%s lev=%s TIF=%s",
             symbol, float(sig.entry_low), float(sig.entry_high),
             str(sig.stop_loss), str(sig.leverage), tif)
    log.info("[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s reduceOnly=%s",
             "BUY" if is_buy else "SELL", coin, px, sz, tif, False)

    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders response: %s", resp)
