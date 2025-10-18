import os, time
from typing import Dict, Any

from .base import BrokerBase

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1","true","yes","on")
BASE = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz")
ACCOUNT_MODE = os.getenv("ACCOUNT_MODE", "spot").lower()

# simple in-memory emulation for DRY_RUN
_emulated_orders: Dict[str, Dict[str, Any]] = {}
_oid = 0

def _mkid(prefix="ord") -> str:
    global _oid
    _oid += 1
    return f"{prefix}_{int(time.time())}_{_oid}"

class HyperliquidBroker(BrokerBase):
    def __init__(self):
        self.key = os.getenv("HYPER_API_KEY", "")
        self.secret = os.getenv("HYPER_API_SECRET", "")

    def get_price(self, symbol: str) -> float:
        # For DRY_RUN, just return a fake price close to 1.0
        return 1.0

    def place_limit(self, symbol: str, side: str, qty: float, price: float, client_id: str) -> str:
        if DRY_RUN:
            oid = _mkid("limit")
            _emulated_orders[oid] = {"status":"open","symbol":symbol,"side":side,"qty":qty,"price":price,"filled":0.0,"client_id":client_id}
            print(f"[DRY] place_limit {symbol} {side} {qty} @ {price} -> {oid}")
            return oid
        # TODO: implement real HTTP call + signing here
        raise NotImplementedError("Implement Hyperliquid place_limit")

    def place_market(self, symbol: str, side: str, qty: float, client_id: str) -> str:
        if DRY_RUN:
            oid = _mkid("mkt")
            _emulated_orders[oid] = {"status":"filled","symbol":symbol,"side":side,"qty":qty,"price":None,"filled":qty,"client_id":client_id}
            print(f"[DRY] place_market {symbol} {side} {qty} -> {oid} (filled)")
            return oid
        raise NotImplementedError("Implement Hyperliquid place_market")

    def place_reduce_only_limit(self, symbol: str, side: str, qty: float, price: float, client_id: str) -> str:
        # For spot, reduce-only is not a concept—executor ensures size correctness.
        return self.place_limit(symbol, side, qty, price, client_id)

    def place_stop(self, symbol: str, side: str, qty: float, stop_price: float, client_id: str) -> str:
        if DRY_RUN:
            oid = _mkid("stop")
            _emulated_orders[oid] = {"status":"open","symbol":symbol,"side":side,"qty":qty,"stop":stop_price,"client_id":client_id}
            print(f"[DRY] place_stop {symbol} {side} {qty} stop={stop_price} -> {oid}")
            return oid
        raise NotImplementedError("Implement Hyperliquid place_stop")

    def order_status(self, order_id: str) -> Dict[str, Any]:
        if DRY_RUN:
            return _emulated_orders.get(order_id, {"status":"unknown"})
        raise NotImplementedError("Implement Hyperliquid order_status")

    def cancel_order(self, order_id: str) -> None:
        if DRY_RUN:
            if order_id in _emulated_orders:
                _emulated_orders[order_id]["status"] = "canceled"
                print(f"[DRY] cancel_order {order_id}")
            return
        raise NotImplementedError("Implement Hyperliquid cancel")

    def open_position_size(self, symbol: str) -> float:
        # Simplify for DRY_RUN:  assume position equals filled - closed
        return 0.0

    def filled_size(self, order_id: str) -> float:
        if DRY_RUN:
            return _emulated_orders.get(order_id, {}).get("filled", 0.0)
        raise NotImplementedError("Implement Hyperliquid filled_size")

