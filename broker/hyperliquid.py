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
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()  # 0x... (API wallet private key)
_ACCOUNT = (os.getenv("HYPER_ACCOUNT_ADDRESS", "") or "").strip()  # 0x... (PUBLIC address)
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
_API_URL = (os.getenv("HYPER_API_URL", "") or "").strip()  # optional override

def _api_url() -> str:
    if _API_URL:
        return _API_URL
    # Default to MAINNET; change to TESTNET_API_URL if you want testnet by default.
    return constants.MAINNET_API_URL

@dataclass
class ExecPlan:
    side: str            # "BUY" | "SELL"
    coin: str            # e.g. "BTC"
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
    """SDK 0.20.x expects: {'limit': {'tif': 'Alo'|'Ioc'|'Gtc'}}; {} for plain limit."""
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

# ----- Entry point the execution layer calls -----
def submit_signal(sig) -> None:
    """
    sig:
      side: 'LONG' | 'SHORT'
      symbol: 'BTC/USD'
      entry_low: float
      entry_high: float
      stop_loss: float | None
      leverage: float | None
      tif: str | None
      notional_usd: float | None
    """
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
    size = notional / limit_px

    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.2f, %.2f) mid=%.2f sz=%.4f SL=%s lev=%s TIF=%s",
        side, symbol, coin, entry_low, entry_high, limit_px, size,
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
        "client_id": None,
    }

    log.info("[HL] SEND bulk_orders: %s", order)
    try:
        resp = ex.bulk_orders([order])
        log.info("[HL] bulk_orders resp: %s", resp)
    except Exception as e:
        log.exception("[HL] ERROR sending bulk_orders: %s", e)
