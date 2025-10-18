import os, time, math
from typing import List, Tuple
from broker.hyperliquid import HyperliquidBroker

TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "100"))
TP_WEIGHTS = [float(x) for x in os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20").split(",")]
POLL_OPEN_ORDERS_SEC = int(os.getenv("POLL_OPEN_ORDERS_SEC", "20"))
ENTRY_TIMEOUT_MIN = int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))

def _qty_from_usd(symbol: str, price: float, usd: float) -> float:
    if price <= 0:
        raise ValueError("Invalid price")
    return usd / price

class Executor:
    def __init__(self):
        self.broker = HyperliquidBroker()

    def execute_signal_oto(
        self,
        symbol: str,
        side: str,                 # "LONG" or "SHORT"
        entry_band: Tuple[float, float],
        stop: float,
        tps: List[float],
    ):
        """Place entry limit in the band mid; after fills, place TPs & SL."""
        side = side.upper()
        is_long = side == "LONG"
        entry_price = (entry_band[0] + entry_band[1]) / 2.0

        # derive qty from USD
        qty = _qty_from_usd(symbol, entry_price, TRADE_SIZE_USD)
        entry_side = "buy" if is_long else "sell"
        reduce_side = "sell" if is_long else "buy"

        entry_id = self.broker.place_limit(symbol, entry_side, qty, entry_price, client_id=f"entry_{symbol}")

        # wait for fill or timeout
        deadline = time.time() + ENTRY_TIMEOUT_MIN * 60
        placed_children_for = 0.0

        while time.time() < deadline:
            filled = self.broker.filled_size(entry_id)
            if filled > placed_children_for + 1e-12:
                new_fill = filled - placed_children_for
                # split new_fill across weights
                for i, price in enumerate(tps[:len(TP_WEIGHTS)]):
                    w = TP_WEIGHTS[i]
                    tp_qty = new_fill * w
                    if tp_qty <= 0:
                        continue
                    self.broker.place_reduce_only_limit(symbol, reduce_side, tp_qty, price, client_id=f"tp{i+1}_{symbol}")

                # stop for the *newly filled* amount
                self.broker.place_stop(symbol, reduce_side, new_fill, stop, client_id=f"sl_{symbol}")
                placed_children_for += new_fill

                # if completely filled, we can exit the watcher (children placed for full size)
                if placed_children_for >= qty - 1e-12:
                    break

            time.sleep(POLL_OPEN_ORDERS_SEC)

        # timeout? cancel entry & exit
        if placed_children_for < qty - 1e-12:
            try:
                self.broker.cancel_order(entry_id)
            except Exception:
                pass

