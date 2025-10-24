# /app/broker/hyperliquid.py
import os
import logging as log
from typing import Any, Dict

try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.wallet import Wallet
except Exception as e:
    raise RuntimeError(
        "Hyperliquid SDK not installed correctly. Expected modules: "
        "hyperliquid.exchange.Exchange, hyperliquid.info.Info, hyperliquid.wallet.Wallet. "
        "Fix requirements/Dockerfile as provided."
    ) from e

ALLOWED = {
    s.strip().upper()
    for s in os.getenv(
        "HYPER_ONLY_EXECUTE_SYMBOLS",
        "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,"
        "MNT/USD,PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD",
    ).split(",")
    if s.strip()
}

DEFAULT_USD = float(os.getenv("HYPER_ORDER_USD", "50"))
TIFS = ["Alo", "PostOnly", "Gtc"]

def _coin(symbol: str) -> str:
    s = symbol.upper().strip()
    return s.split("/", 1)[0] if "/" in s else s

def _entry_mid(sig) -> float:
    lo = float(getattr(sig, "entry_low"))
    hi = float(getattr(sig, "entry_high"))
    return (lo + hi) / 2.0

def _usd_to_sz(usd: float, px: float) -> float:
    return usd / px

def _round(x: float, dec: int) -> float:
    return float(f"{x:.{dec}f}")

def _build_order(coin: str, is_buy: bool, sz: float, px: float, tif: str) -> Dict[str, Any]:
    ot = {"limit": {"tif": tif}}
    return {
        "coin": coin,
        "is_buy": bool(is_buy),
        "sz": float(sz),
        "limit_px": float(px),
        "order_type": ot,     # new-style key
        "orderType": ot,      # compat key
        "reduce_only": False,
        "reduceOnly": False,
    }

def _bulk_with_retries(ex: Exchange, order: Dict[str, Any]) -> Any:
    base_px = float(order["limit_px"])
    base_sz = float(order["sz"])
    last = None
    for tif in TIFS:
        order["order_type"]["limit"]["tif"] = tif
        order["orderType"]["limit"]["tif"] = tif
        for dec in range(8, 1, -1):
            order["limit_px"] = _round(base_px, dec)
            order["sz"] = _round(base_sz, max(dec - 2, 0))
            try:
                return ex.bulk_orders([order])
            except Exception as e:
                msg = str(e)
                last = e
                if any(k in msg for k in (
                    "float_to_wire", "rounding", "Invalid order type",
                    "Unknown format code 'f'", "SignableMessage", "sign_message",
                    "byte indices must be integers"
                )):
                    continue
                raise
    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last}")

def submit_signal(sig) -> None:
    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not (priv and priv.startswith("0x")):
        raise RuntimeError("Set HYPER_PRIVATE_KEY to your 0x-prefixed EVM private key.")

    wallet = Wallet.from_key(priv)
    ex = Exchange(wallet=wallet)
    info = Info()

    side = str(getattr(sig, "side", "")).upper()
    symbol = str(getattr(sig, "symbol", "")).upper()
    if not side or not symbol:
        raise ValueError("Signal missing side or symbol.")
    if ALLOWED and symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol (not allowed): %s", symbol)
        return

    px = _entry_mid(sig)
    usd = float(os.getenv("HYPER_ORDER_USD", DEFAULT_USD))
    sz = _usd_to_sz(usd, px)

    is_buy = (side == "LONG")
    coin = _coin(symbol)

    log.info("[BROKER] %s %s band=(%s,%s) SL=%s lev=%s",
             "BUY" if is_buy else "SELL", symbol,
             getattr(sig, "entry_low", None),
             getattr(sig, "entry_high", None),
             getattr(sig, "stop_loss", None),
             getattr(sig, "leverage", None))

    order = _build_order(coin, is_buy, sz, px, "Alo")
    log.info("[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s",
             "BUY" if is_buy else "SELL", coin, order["limit_px"], order["sz"],
             order["order_type"]["limit"]["tif"])

    resp = _bulk_with_retries(ex, order)
    log.info("[BROKER] bulk_orders response: %s", resp)
