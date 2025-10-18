import os, time, functools
from typing import Dict, Any, Optional, Set
from .base import BrokerBase

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1","true","yes","on")
BASE = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz")
ACCOUNT_MODE = os.getenv("ACCOUNT_MODE", "perp").lower()

# ---- DRY-RUN emulation storage ----
_emulated_orders: Dict[str, Dict[str, Any]] = {}
_oid = 0

def _mkid(prefix="ord") -> str:
    global _oid
    _oid += 1
    return f"{prefix}_{int(time.time())}_{_oid}"

# Optional symbol mapping for name mismatches
SYMBOL_MAP = {
    "1000BONK/USDT": "BONK/USDT",
}

def _normalize_for_hl(symbol: str) -> str:
    return SYMBOL_MAP.get(symbol.upper(), symbol.upper())

class HyperliquidBroker(BrokerBase):
    def __init__(self):
        self.key = os.getenv("HYPER_API_KEY", "")
        self.secret = os.getenv("HYPER_API_SECRET", "")

    # ---------- discovery / allow-list ----------
    @functools.lru_cache(maxsize=1)
    def list_symbols(self) -> Set[str]:
        allow = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "")
        if allow.strip():
            return {s.strip().upper() for s in allow.split(",") if s.strip()}
        # If you want to auto-fetch markets, implement it here via HTTP and return a set
        # For now, DRY defaults:
        return {"BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT","DOGE/USDT","LINK/USDT","ADA/USDT","AVAX/USDT"}

    def supports_symbol(self, symbol: str) -> bool:
        return _normalize_for_hl(symbol) in self.list_symbols()

    # ---------- price ----------
    def get_price(self, symbol: str) -> float:
        # Implement price fetch if you want. For DRY, just return 1.0
        return 1.0

    # ---------- orders ----------
    def place_limit(self, symbol: str, side: str, qty: float, price: float,
                    client_id: str, leverage: Optional[float] = None) -> str:
        symbol = _normalize_for_hl(symbol)
        if DRY_RUN:
            oid = _mkid("limit")
            _emulated_orders[oid] = {
                "status":"open","symbol":symbol,"side":side,"qty":qty,
                "price":price,"filled":0.0,"client_id":client_id,"lev":leverage
            }
            print(f"[DRY] LIMIT {symbol} {side} {qty} @ {price} lev={leverage} -> {oid}")
            return oid
        # TODO: implement real REST call with auth to Hyperliquid
        raise NotImplementedError("Implement Hyperliquid limit order")

    def place_market(self, symbol: str, side: str, qty: float,
                     client_id: str, leverage: Optional[float] = None) -> str:
        symbol = _normalize_for_hl(symbol)
        if DRY_RUN:
            oid = _mkid("mkt")
            _emulated_orders[oid] = {
                "status":"filled","symbol":symbol,"side":side,"qty":qty,
                "price":None,"filled":qty,"client_id":client_id,"lev":leverage
            }
            print(f"[DRY] MARKET {symbol} {side} {qty} lev={leverage} -> {oid} (filled)")
            return oid
        raise NotImplementedError("Implement Hyperliquid market order")

    def place_reduce_only_limit(self, symbol: str, side: str, qty: float, price: float,
                                client_id: str, leverage: Optional[float] = None) -> str:
        # On spot, reduce-only isn't a concept; executor ensures sizing. For perps, you'd set reduceOnly=true
        return self.place_limit(symbol, side, qty, price, client_id, leverage)

    def place_stop(self, symbol: str, side: str, qty: float, stop_price: float,
                   client_id: str, leverage: Optional[float] = None) -> str:
        symbol = _normalize_for_hl(symbol)
        if DRY_RUN:
            oid = _mkid("stop")
            _emulated_orders[oid] = {
                "status":"open","symbol":symbol,"side":side,"qty":qty,
                "stop":stop_price,"client_id":client_id,"lev":leverage
            }
            print(f"[DRY] STOP  {symbol} {side} {qty} stop={stop_price} lev={leverage} -> {oid}")
            return oid
        raise NotImplementedError("Implement Hyperliquid stop/trigger")

    def order_status(self, order_id: str) -> Dict[str, Any]:
        if DRY_RUN:
            return _emulated_orders.get(order_id, {"status":"unknown"})
        raise NotImplementedError("Implement Hyperliquid order_status")

    def cancel_order(self, order_id: str) -> None:
        if DRY_RUN:
            if order_id in _emulated_orders:
                _emulated_orders[order_id]["status"] = "canceled"
                print(f"[DRY] CANCEL {order_id}")
            return
        raise NotImplementedError("Implement Hyperliquid cancel_order")

    def filled_size(self, order_id: str) -> float:
        if DRY_RUN:
            return _emulated_orders.get(order_id, {}).get("filled", 0.0)
        raise NotImplementedError("Implement Hyperliquid filled_size")
