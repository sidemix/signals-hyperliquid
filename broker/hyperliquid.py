import os
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)
log.propagate = False

# ----- Config -----
_ALLOWED = set(s.strip().upper() for s in (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "") or "").split(",") if s.strip())
_DEFAULT_TIF = (os.getenv("HYPER_TIF", "Alo") or "").strip()
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()
_ACCOUNT = (os.getenv("HYPER_ACCOUNT_ADDRESS", "") or "").strip()
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
_API_URL = (os.getenv("HYPER_API_URL", "") or "").strip()

# ---- Cross-process idempotency (SQLite) ----
_IDEMP_DB_PATH = os.getenv("IDEMP_DB_PATH", "/tmp/hyper_idempotency.db")
_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", "86400"))  # 24h

def _idemp_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_IDEMP_DB_PATH, timeout=10, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_client_ids (
            client_id TEXT PRIMARY KEY,
            ts INTEGER NOT NULL
        )
    """)
    return conn

def _purge_old(conn: sqlite3.Connection, now: int) -> None:
    try:
        conn.execute("DELETE FROM sent_client_ids WHERE ts < ?", (now - _IDEMP_TTL_SECS,))
    except Exception:
        pass

def _idempotent_claim(client_id: Optional[str]) -> bool:
    """Atomically claim a client_id. True = first sender, False = duplicate."""
    if not client_id:
        return True
    conn = None
    try:
        now = int(time.time())
        conn = _idemp_conn()
        _purge_old(conn, now)
        conn.execute("INSERT INTO sent_client_ids (client_id, ts) VALUES (?, ?)", (client_id, now))
        log.info("[HL] IDEMP: claimed client_id=%s", client_id)
        return True
    except sqlite3.IntegrityError:
        log.info("[HL] SKIP duplicate (cross-process) client_id=%s", client_id)
        return False
    except Exception as e:
        log.exception("[HL] Idempotency DB error (proceeding best-effort): %s", e)
        return True
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

# Per-process guard (same interpreter)
_SENT_CLIENT_IDS: set[str] = set()

def _api_url() -> str:
    return _API_URL or constants.MAINNET_API_URL

@dataclass
class ExecPlan:
    side: str
    coin: str
    limit_px: float
    size: float
    tif: Optional[str]
    reduce_only: bool = False

# ---------- Helpers ----------
def _require_signer():
    if not _PRIVKEY:
        raise RuntimeError("Set HYPER_PRIVATE_KEY (0x... private key).")
    if not _ACCOUNT:
        raise RuntimeError("Set HYPER_ACCOUNT_ADDRESS (0x... public address).")
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

def _order_type_for_tif(tif: Optional[str]) -> dict:
    if not tif:
        return {}
    t = tif.strip().lower()
    if t in ("postonly", "alo"): return {"limit": {"tif": "Alo"}}
    if t == "ioc":               return {"limit": {"tif": "Ioc"}}
    if t == "gtc":               return {"limit": {"tif": "Gtc"}}
    return {}

def _quantize_down(x: float, step: float) -> float:
    if step <= 0:
        return x
    return (int(x / step)) * step

# ---- Robust asset metadata resolution across SDK layouts ----
def _try_get_assets_container(info: Info) -> Optional[Any]:
    cont = getattr(info, "assets", None)
    if isinstance(cont, (list, tuple)) and cont and isinstance(cont[0], dict):
        return cont
    meta = getattr(info, "meta", None)
    if isinstance(meta, dict):
        cont = meta.get("assets")
        if isinstance(cont, (list, tuple)) and cont and isinstance(cont[0], dict):
            return cont
    try:
        for v in info.__dict__.values():
            if isinstance(v, (list, tuple)) and v and isinstance(v[0], dict) and "pxDecimals" in v[0]:
                return v
    except Exception:
        pass
    return None

def _resolve_asset_dict(info: Info, coin: str) -> Optional[dict]:
    try:
        res = info.name_to_asset(coin)
        if isinstance(res, dict): return res
        if isinstance(res, int):
            container = _try_get_assets_container(info)
            if container and 0 <= res < len(container):
                return container[res]
    except Exception:
        pass
    container = _try_get_assets_container(info)
    if isinstance(container, (list, tuple)):
        for d in container:
            nm = d.get("name") or d.get("token") or d.get("symbol")
            if isinstance(nm, str) and nm.upper() == coin.upper():
                return d
    return None

def _get_asset_meta(info: Info, coin: str) -> tuple[float, float, Optional[float]]:
    price_tick = 0.01
    size_step = 1.0
    min_sz: Optional[float] = None
    asset = _resolve_asset_dict(info, coin)
    if asset:
        try:
            px_dec = int(asset.get("pxDecimals", asset.get("px_decimals", 2)))
            sz_dec = int(asset.get("szDecimals", asset.get("sz_decimals", 0)))
            price_tick = 10 ** (-px_dec) if px_dec >= 0 else price_tick
            size_step = 10 ** (-sz_dec) if sz_dec >= 0 else size_step
        except Exception:
            pass
        for k in ("minSz", "minSize", "min_size"):
            if k in asset:
                try:
                    min_sz = float(asset[k])
                except Exception:
                    pass
                break
    return price_tick, size_step, min_sz

# ---------- Main ----------
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
    mid = (entry_low + entry_high) / 2.0

    # Build client_id as early as possible and claim it BEFORE network calls
    client_id = getattr(sig, "client_id", None)

    # Process-local guard
    if client_id:
        if client_id in _SENT_CLIENT_IDS:
            log.info("[HL] SKIP duplicate (process-local) client_id=%s", client_id)
            return
        # Cross-process guard
        if not _idempotent_claim(client_id):
            return
        _SENT_CLIENT_IDS.add(client_id)

    ex, info = _mk_clients()
    price_tick, size_step, min_sz = _get_asset_meta(info, coin)

    limit_px = _quantize_down(mid, price_tick)
    override = getattr(sig, "notional_usd", None)
    notional = float(override) if override is not None else _DEFAULT_NOTIONAL
    raw_size = (notional / limit_px) if limit_px > 0 else 0.0
    size = _quantize_down(raw_size, size_step)

    if min_sz is not None and size < min_sz:
        log.info("[HL] SKIP: computed size %.10f < min size %.10f for %s", size, min_sz, coin)
        return
    if size <= 0.0:
        log.info("[HL] SKIP: non-positive size after rounding: %.10f (coin=%s)", size, coin)
        return

    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.6f, %.6f) mid=%.6f pxTick=%g szStep=%g minSz=%s sz=%.10f SL=%s lev=%s TIF=%s",
        side, symbol, coin, entry_low, entry_high, mid, price_tick, size_step,
        "None" if min_sz is None else f"{min_sz}", size, getattr(sig, "stop_loss", None),
        getattr(sig, "leverage", None), tif
    )

    order = {
        "coin": coin,
        "is_buy": (side == "BUY"),
        "sz": float(size),
        "limit_px": float(limit_px),
        "order_type": _order_type_for_tif(tif),
        "reduce_only": False,
        "client_id": client_id,
    }

    log.info("[HL] SEND bulk_orders: %s", order)
    try:
        resp = ex.bulk_orders([order])
        log.info("[HL] bulk_orders resp: %s", resp)
    except Exception as e:
        log.exception("[HL] ERROR sending bulk_orders: %s", e)
