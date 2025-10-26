# hyperliquid.py
import os
import sys
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Any, Iterable

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ---- Logging to stdout (so you see [HL] lines in Render)
log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    log.addHandler(_h)
log.propagate = False

# ----- Config -----
_ALLOWED = set(s.strip().upper() for s in (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "") or "").split(",") if s.strip())
_DEFAULT_TIF = (os.getenv("HYPER_TIF", "Alo") or "").strip()
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()
_ACCOUNT = (os.getenv("HYPER_ACCOUNT_ADDRESS", "") or "").strip()
_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
_API_URL = (os.getenv("HYPER_API_URL", "") or "").strip()

# Safer fallbacks so high-price coins don't zero out
_FALLBACK_PRICE_TICK = float(os.getenv("HYPER_FALLBACK_PRICE_TICK", "0.01"))
_FALLBACK_SIZE_STEP  = float(os.getenv("HYPER_FALLBACK_SIZE_STEP",  "0.001"))
_FALLBACK_MIN_SIZE   = os.getenv("HYPER_FALLBACK_MIN_SIZE", "").strip()
_FALLBACK_MIN_SIZE   = float(_FALLBACK_MIN_SIZE) if _FALLBACK_MIN_SIZE else None

# ---- Idempotency storage (SQLite; good inside one container)
_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", "86400"))  # 24h
_IDEMP_DB_PATH = os.getenv("IDEMP_DB_PATH", "/tmp/hyper_idempotency.db")
_IDEMP_LOCKFILE = os.getenv("IDEMP_LOCKFILE", "/tmp/hyper_idempotency.lock")

# Optional Redis (for cross-container idempotency)
_IDEMP_REDIS_URL = os.getenv("IDEMP_REDIS_URL", "").strip()
_redis = None
if _IDEMP_REDIS_URL:
    try:
        import redis
        _redis = redis.Redis.from_url(_IDEMP_REDIS_URL, decode_responses=True)
    except Exception:
        _redis = None

# Optional file lock (Linux)
_filelock_supported = True
try:
    import fcntl
except Exception:
    _filelock_supported = False

# Process-local guard
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
            if isinstance(v, (list, tuple)) and v and isinstance(v[0], dict) and ("pxDecimals" in v[0] or "px_decimals" in v[0]):
                return v
    except Exception:
        pass
    return None

def _resolve_asset_dict(info: Info, coin: str) -> Optional[dict]:
    try:
        res = info.name_to_asset(coin)
        if isinstance(res, dict):
            return res
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
    price_tick = _FALLBACK_PRICE_TICK
    size_step = _FALLBACK_SIZE_STEP
    min_sz: Optional[float] = _FALLBACK_MIN_SIZE

    asset = _resolve_asset_dict(info, coin)
    if asset:
        try:
            px_dec = int(asset.get("pxDecimals", asset.get("px_decimals", 2)))
            sz_dec = int(asset.get("szDecimals", asset.get("sz_decimals", 3)))  # safer default 3
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

# ---------- Idempotency (Redis → SQLite+lock) ----------
def _redis_claim(client_id: str) -> Optional[bool]:
    if not _redis:
        return None
    try:
        ok = _redis.set(name=f"hl:idemp:{client_id}", value="1", nx=True, ex=_IDEMP_TTL_SECS)
        if ok:
            log.info("[HL] IDEMP[redis]: claimed %s", client_id)
            return True
        else:
            log.info("[HL] SKIP duplicate (redis) %s", client_id)
            return False
    except Exception as e:
        log.exception("[HL] Redis idempotency error (falling back): %s", e)
        return None

def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_IDEMP_DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_client_ids (
            client_id TEXT PRIMARY KEY,
            ts INTEGER NOT NULL
        )
    """)
    return conn

def _sqlite_claim(client_id: str) -> bool:
    now = int(time.time())
    conn = None
    lockf = None
    try:
        if _filelock_supported:
            lockf = open(_IDEMP_LOCKFILE, "a+")
            fcntl.flock(lockf, fcntl.LOCK_EX)
        conn = _sqlite_conn()
        conn.execute("DELETE FROM sent_client_ids WHERE ts < ?", (now - _IDEMP_TTL_SECS,))
        conn.execute("INSERT INTO sent_client_ids (client_id, ts) VALUES (?, ?)", (client_id, now))
        log.info("[HL] IDEMP[sqlite]: claimed %s", client_id)
        return True
    except sqlite3.IntegrityError:
        log.info("[HL] SKIP duplicate (sqlite) %s", client_id)
        return False
    except Exception as e:
        log.exception("[HL] SQLite idempotency error (best-effort continue): %s", e)
        return True
    finally:
        try:
            if conn: conn.close()
        except Exception:
            pass
        if _filelock_supported and lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()
            except Exception:
                pass

def _claim_client_id(client_id: Optional[str]) -> bool:
    if not client_id:
        return True
    if client_id in _SENT_CLIENT_IDS:
        log.info("[HL] SKIP duplicate (process) %s", client_id)
        return False
    res = _redis_claim(client_id)
    if res is True:
        _SENT_CLIENT_IDS.add(client_id)
        return True
    if res is False:
        return False
    if _sqlite_claim(client_id):
        _SENT_CLIENT_IDS.add(client_id)
        return True
    return False

# ---------- Open-order duplicate check ----------
def _iter_open_orders(info: Info) -> Iterable[dict]:
    for attr in ("open_orders", "user_open_orders"):
        fn = getattr(info, attr, None)
        if callable(fn):
            try:
                oo = fn()
                if isinstance(oo, (list, tuple)):
                    for x in oo:
                        if isinstance(x, dict):
                            yield x
                elif isinstance(oo, dict):
                    for arr in oo.values():
                        if isinstance(arr, (list, tuple)):
                            for x in arr:
                                if isinstance(x, dict): yield x
            except Exception:
                pass
    state = None
    for attr in ("user_state", "userState", "account_state", "accountState"):
        fn = getattr(info, attr, None)
        if callable(fn):
            try:
                state = fn()
                break
            except Exception:
                pass
    if isinstance(state, dict):
        for key in ("openOrders", "open_orders", "orders"):
            arr = state.get(key)
            if isinstance(arr, (list, tuple)):
                for x in arr:
                    if isinstance(x, dict): yield x

def _order_matches(o: dict, coin: str, is_buy: bool, limit_px: float, size: float) -> bool:
    try:
        o_coin = o.get("coin") or o.get("asset") or o.get("symbol")
        o_isbuy = (o.get("isBuy") if "isBuy" in o else o.get("is_buy"))
        o_px = o.get("px", o.get("price"))
        o_sz = o.get("sz", o.get("size"))
        if isinstance(o_isbuy, str):
            o_isbuy = o_isbuy.lower() in ("true", "1", "yes", "buy", "long")
        o_px = float(o_px) if o_px is not None else None
        o_sz = float(o_sz) if o_sz is not None else None
        if (o_coin or "").upper() != coin.upper():
            return False
        if bool(o_isbuy) != bool(is_buy):
            return False
        if o_px is None or abs(o_px - float(limit_px)) > 1e-12:
            return False
        if o_sz is None or abs(o_sz - float(size)) > 1e-9:
            return False
        return True
    except Exception:
        return False

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

    client_id = getattr(sig, "client_id", None)
    if not _claim_client_id(client_id):
        return

    ex, info = _mk_clients()
    price_tick, size_step, min_sz = _get_asset_meta(info, coin)

    # Base limit at mid
    limit_px = _quantize_down(mid, price_tick)

    # ALO nudge: move 1 tick AWAY from crossing so PostOnly won't reject
    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)
    tif_map = _order_type_for_tif(tif)
    if tif_map.get("limit", {}).get("tif") == "Alo":
        if side == "BUY":
            limit_px = _quantize_down(max(price_tick, limit_px - price_tick), price_tick)
        else:  # SELL
            limit_px = _quantize_down(limit_px + price_tick, price_tick)
        log.info("[HL] ALO nudge applied: limit_px=%s tick=%s side=%s", limit_px, price_tick, side)

    override = getattr(sig, "notional_usd", None)
    notional = float(override) if override is not None else _DEFAULT_NOTIONAL
    raw_size = (notional / limit_px) if limit_px > 0 else 0.0
    size = _quantize_down(raw_size, size_step)

    # Floor rescue for rounding-to-zero
    min_floor = min_sz if min_sz is not None else (_FALLBACK_MIN_SIZE if _FALLBACK_MIN_SIZE is not None else 0.0)
    if size < min_floor and raw_size >= (min_floor if min_floor > 0 else _FALLBACK_SIZE_STEP):
        size = max(min_floor, _FALLBACK_SIZE_STEP)

    if min_sz is not None and size < min_sz:
        log.info("[HL] SKIP: size %.10f < min %.10f for %s (raw=%.10f step=%g px=%.6f notional=%.2f)",
                 size, min_sz, coin, raw_size, size_step, limit_px, notional)
        return
    if size <= 0.0:
        log.info("[HL] SKIP: non-positive size %.10f (raw=%.10f step=%g coin=%s px=%.6f notional=%.2f)",
                 size, raw_size, size_step, coin, limit_px, notional)
        return

    is_buy = (side == "BUY")

    # Safety net: skip if identical order already on book
    try:
        for o in _iter_open_orders(info):
            if _order_matches(o, coin=coin, is_buy=is_buy, limit_px=limit_px, size=size):
                log.info("[HL] SKIP: identical open order already exists on book: %s", o)
                return
    except Exception as e:
        log.warning("[HL] open-order duplicate check failed (continuing): %s", e)

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.6f, %.6f) mid=%.6f pxTick=%g szStep=%g minSz=%s sz=%.10f SL=%s lev=%s TIF=%s client_id=%s",
        side, symbol, coin, entry_low, entry_high, mid, price_tick, size_step,
        "None" if min_sz is None else f"{min_sz}", size, getattr(sig, "stop_loss", None),
        getattr(sig, "leverage", None), tif_map, client_id
    )

    order = {
        "coin": coin,
        "is_buy": is_buy,
        "sz": float(size),
        "limit_px": float(limit_px),
        "order_type": tif_map,
        "reduce_only": False,
        "client_id": client_id,
    }

    log.info("[HL] SEND bulk_orders: %s", order)
    try:
        resp = ex.bulk_orders([order])
        log.info("[HL] bulk_orders resp raw: %s", resp)

        # Try to detect explicit rejections
        try:
            # SDKs vary; handle common shapes
            if isinstance(resp, dict):
                errs = resp.get("errors") or resp.get("error")
                oks  = resp.get("data") or resp.get("success") or resp.get("orderResponses")
                if errs:
                    log.warning("[HL] REJECT by exchange: %s", errs)
                elif isinstance(oks, list) and oks and isinstance(oks[0], dict):
                    s = oks[0].get("status") or oks[0].get("result")
                    if s and str(s).lower() not in ("ok", "accepted", "success"):
                        log.warning("[HL] NON-OK order status: %s", s)
        except Exception:
            pass

    except Exception as e:
        log.exception("[HL] ERROR sending bulk_orders: %s", e)
