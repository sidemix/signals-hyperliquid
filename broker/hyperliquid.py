# broker/hyperliquid.py
import os
import logging
from dataclasses import dataclass

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)

# ----- Config -----
_ALLOWED = set(s.strip().upper() for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",") if s.strip())
_DEFAULT_TIF = (os.getenv("HYPER_TIF", "Alo") or "").strip()  # Alo | Ioc | Gtc (PostOnly ~= Alo)
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()
_ACCOUNT = (os.getenv("HYPER_ACCOUNT_ADDRESS", "") or "").strip()
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
_API_URL = (os.getenv("HYPER_API_URL", "") or "").strip()


def _api_url() -> str:
    if _API_URL:
        return _API_URL
    return constants.MAINNET_API_URL


@dataclass
class ExecPlan:
    side: str
    coin: str
    limit_px: float
    size: float
    tif: str | None
    reduce_only: bool = False


# ----- Helpers -----
def _require_signer():
    if not _PRIVKEY:
        raise RuntimeError("Set HYPER_PRIVATE_KEY (0x... API wallet private key).")
    if not _ACCOUNT:
        raise RuntimeError("Set HYPER_ACCOUNT_ADDRESS (your public address, 0x...).")
    try:
        return Account.from_key(_PRIVKEY)
    except Exception as e:
        raise RuntimeError(f"Invalid HYPER_PRIVATE_KEY: {e}")


def _mk_clients() -> tuple[Exchange, Info]:
    signer = _require_signer()
    url = _api_url()
    ex = Exchange(signer, url, account_address=_ACCOUNT)
    info = Info(url, skip_ws=True)
    return ex, info


def _coin_from_symbol(symbol: str) -> str:
    return (symbol or "").split("/")[0].upper()


def _symbol_ok(symbol: str) -> bool:
    if not _ALLOWED:
        return True
    sym_up = (symbol or "").upper()
    coin = _coin_from_symbol(symbol)
    return sym_up in _ALLOWED or coin in _ALLOWED


def _order_type_for_tif(tif: str | None) -> dict:
    if not tif:
        return {}
    t = tif.strip().lower()
    if t in ("postonly", "alo"):
        return {"limit": {"tif": "Alo"}}
    if t == "ioc":
        return {"limit": {"tif": "Ioc"}}
    if t == "gtc":
        return {"limit": {"tif": "Gtc"}}
    return {}


# ----- Core logic -----
def _tick_size_for_coin(coin: str) -> float:
    """Approximate tick sizes per coin. Adjust as needed."""
    tick_map = {
        "BTC": 0.5,
        "ETH": 0.05,
        "SOL": 0.001,
        "LINK": 0.001,
        "BNB": 0.01,
        "AVAX": 0.001,
        "PAXG": 0.1,
        "SNX": 0.001,
        "MNT": 0.001,
        "CRV": 0.0001,
    }
    return tick_map.get(coin.upper(), 0.01)  # default fallback


def _round_to_tick(value: float, tick: float) -> float:
    """Round value to the nearest valid tick."""
    return round(round(value / tick) * tick, 10)


def submit_signal(sig) -> None:
    if sig is None:
        raise ValueError("submit_signal(sig): sig is None")

    entry_low = getattr(sig, "entry_low", None)
    entry_high = getattr(sig, "entry_high", None)
    if entry_low is None or entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol = getattr(sig, "symbol", "") or ""
    if not _symbol_ok(symbol):
        log.info("[HL] SKIP: %s not in HYPER_ONLY_EXECUTE_SYMBOLS=%s", symbol, sorted(_ALLOWED))
        return

    side_raw = (getattr(sig, "side", "") or "").upper()
    if side_raw not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side '{sig.side}'. Expected LONG or SHORT.")
    side = "BUY" if side_raw == "LONG" else "SELL"

    coin = _coin_from_symbol(symbol)
    entry_low = float(entry_low)
    entry_high = float(entry_high)
    limit_px = (entry_low + entry_high) / 2.0
    if limit_px <= 0:
        raise ValueError(f"Computed limit_px <= 0 for {symbol}: {limit_px}")

    # --- FIXED: handle None safely ---
    _override = getattr(sig, "notional_usd", None)
    notional = float(_override) if _override is not None else _DEFAULT_NOTIONAL

    # --- FIXED: round size & price to valid increments ---
    raw_size = notional / limit_px
    size = round(raw_size, 5)  # round to 1e-5
    if size <= 0:
        raise ValueError(f"Computed trade size <= 0 for {symbol}: {raw_size}")

    tick = _tick_size_for_coin(coin)
    limit_px = _round_to_tick(limit_px, tick)

    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.2f, %.2f) mid=%.2f tick=%.4f sz=%.5f SL=%s lev=%s TIF=%s",
        side, symbol, coin, entry_low, entry_high, limit_px, tick, size,
        getattr(sig, "stop_loss", None), getattr(sig, "leverage", None), tif
    )

    ex, _info = _mk_clients()
    order = {
        "coin": coin,
        "is_buy": (side == "BUY"),
        "sz": float(size),
        "limit_px": float(limit_px),
        "order_type": _order_type_for_tif(tif),
        "reduce_only": False,
        "client_id": getattr(sig, "client_id", None),  # must NOT be None if set in listener
    }

    log.info("[HL] SEND bulk_orders: %s", order)



    log.info("[HL] SEND bulk_orders: %s", order)
    try:
        resp = ex.bulk_orders([order])
        log.info("[HL] bulk_orders resp: %s", resp)
    except Exception as e:
        log.exception("[HL] ERROR sending bulk_orders: %s", e)
