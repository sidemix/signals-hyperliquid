import os, time
from typing import List
from broker.hyperliquid import HyperliquidBroker
from parser import Signal

TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "100"))
TP_WEIGHTS = [float(x) for x in os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20").split(",")]
POLL_OPEN_ORDERS_SEC = int(os.getenv("POLL_OPEN_ORDERS_SEC", "20"))
ENTRY_TIMEOUT_MIN = int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))
ACCOUNT_MODE = os.getenv("ACCOUNT_MODE", "perp").lower()

FORCE_LEVERAGE = float(os.getenv("FORCE_LEVERAGE", "0"))
MAX_LEVERAGE = float(os.getenv("MAX_LEVERAGE", "0"))  # 0 = no cap

def _qty_from_usd(price: float, usd: float) -> float:
    if price <= 0:
        raise ValueError("Invalid price")
    return usd / price

def _effective_leverage(sig_lev: float | None) -> float | None:
    # priority: force -> cap -> signal
    if FORCE_LEVERAGE and FORCE_LEVERAGE > 0:
        return FORCE_LEVERAGE
    lev = sig_lev if sig_lev and sig_lev > 0 else None
    if lev and MAX_LEVERAGE and MAX_LEVERAGE > 0 and lev > MAX_LEVERAGE:
        lev = MAX_LEVERAGE
    return lev

class Executor:
    def __init__(self):
        self.broker = HyperliquidBroker()

    def execute_signal_oto(self, sig: Signal):
        symbol = sig.symbol
        if not self.broker.supports_symbol(symbol):
            print(f"[EXEC] skip {symbol} — not listed on Hyperliquid")
            return

        is_long = sig.side.upper() == "LONG"
        entry_price = (sig.entry_band[0] + sig.entry_band[1]) / 2.0
        qty = _qty_from_usd(entry_price, TRADE_SIZE_USD)

        entry_side = "buy" if is_long else "sell"
        exit_side  = "sell" if is_long else "buy"
        leverage   = _effective_leverage(sig.leverage)

        # 1) place entry limit
        entry_id = self.broker.place_limit(
            symbol, entry_side, qty, entry_price, client_id=f"entry_{symbol}",
            leverage=leverage
        )

        # 2) wait for fills; place children per fill delta
        deadline = time.time() + ENTRY_TIMEOUT_MIN * 60
        placed_for = 0.0

        while time.time() < deadline:
            filled = self.broker.filled_size(entry_id)
            if filled > placed_for + 1e-12:
                delta = filled - placed_for
                # TPs (split by weights)
                for i, price in enumerate(sig.take_profits[:len(TP_WEIGHTS)]):
                    tp_qty = delta * TP_WEIGHTS[i]
                    if tp_qty <= 0:
                        continue
                    self.broker.place_reduce_only_limit(
                        symbol, exit_side, tp_qty, price,
                        client_id=f"tp{i+1}_{symbol}", leverage=leverage
                    )
                # SL for the newly filled size
                self.broker.place_stop(
                    symbol, exit_side, delta, sig.stop,
                    client_id=f"sl_{symbol}", leverage=leverage
                )
                placed_for += delta
                if placed_for >= qty - 1e-12:
                    break
            time.sleep(POLL_OPEN_ORDERS_SEC)

        if placed_for < qty - 1e-12:
            # timeout: cancel stale entry
            try:
                self.broker.cancel_order(entry_id)
            except Exception:
                pass
