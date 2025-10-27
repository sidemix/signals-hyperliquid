import os, sys, logging, sqlite3, time
from dataclasses import dataclass
from typing import Optional, Any, Iterable

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    log.addHandler(_h)
log.propagate = False

# ----- Config -----
_ALLOWED = set(
    s.strip().upper()
    for s in (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "") or "").split(",")
    if s.strip()
)
_DEFAULT_TIF = (os.getenv("HYPER_TIF", "Alo") or "").strip()
_PRIVKEY = (os.getenv("HYPER_PRIVATE_KEY", "") or "").strip()
_ACCOUNT = (os.getenv("HYPER_ACCOUNT_ADDRESS", "") or "").strip()

_DEFAULT_NOTIONAL = float(os.getenv("HYPER_NOTIONAL_USD", "50"))
_FIXED_QTY = float(os.getenv("HYPER_FIXED_QTY", "0") or 0) or None

_API_URL = (os.getenv("HYPER_API_URL", "") or "").strip()

_FALLBACK_PRICE_TICK = float(os.getenv("HYPER_FALLBACK_PRICE_TICK", "0.01"))
_FALLBACK_SIZE_STEP = float(os.getenv("HYPER_FALLBACK_SIZE_STEP", "0.001"))
_FALLBACK_MIN_SIZE = float(os.getenv("HYPER_FALLBACK_MIN_SIZE", "0") or 0) or None

_IDEMP_TTL_SECS = int(os.getenv("IDEMP_TTL_SECS", "86400"))
_IDEMP_DB_PATH = os.getenv("IDEMP_DB_PATH", "/tmp/hyper_idempotency.db")
_IDEMP_LOCKFILE = os.getenv("IDEMP_LOCKFILE", "/tmp/hyper_idempotency.lock")

_IDEMP_REDIS_URL = os.getenv("IDEMP_REDIS_URL", "").strip()
_redis = None
if _IDEMP_REDIS_URL:
    try:
        import redis

        _redis = redis.Redis.from_url(_IDEMP_REDIS_URL, decode_responses=True)
    except Exception:
        _redis = None

_filelock_supported = True
try:
    import fcntl
except Exception:
    _filelock_supported = False

_SENT_CLIENT_IDS: set[str] = set()

# ---- TP/SL feature toggles ----
_PLACE_TPSL = (os.getenv("HYPER_PLACE_TPSL", "false") or "").lower() in ("1", "true", "yes")
_TP_SPLIT_MODE = (os.getenv("HYPER_TP_SPLIT_MODE", "equal") or "equal").lower()
_TP_SPLIT_RATIO_RAW = (os.getenv("HYPER_TP_SPLIT_RATIO", "") or "").strip()
_DEFAULT_TP_PXS_RAW = (os.getenv("HYPER_DEFAULT_TP_PXS", "") or "").strip()


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


def _require_signer():
    if not _PRIVKEY:
        raise RuntimeError("Set HYPER_PRIVATE_KEY")
    if not _ACCOUNT:
        raise RuntimeError("Set HYPER_ACCOUNT_ADDRESS")
    try:
        return Account.from_key(_PRIVKEY)
    except Exception as e:
        raise RuntimeError(f"Invalid HYPER_PRIVATE_KEY: {e}")


def _mk_clients() -> tuple[Exchange, Info]:
    signer = _require_signer()
    url = _api_url()
    return Exchange(signer, url, account_address=_ACCOUNT), Info(url, skip_ws=True)


def _coin_from_symbol(symbol: str) -> str:
    return (symbol or "").split("/")[0].upper()


def _symbol_ok(symbol: str) -> bool:
    if not _ALLOWED:
        return True
    sym = (symbol or "").upper()
    coin = _coin_from_symbol(symbol)
    return sym in _ALLOWED or coin in _ALLOWED


def _order_type_for_tif(tif: Optional[str]) -> dict:
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


def _quantize_down(x: float, step: float) -> float:
    if step <= 0:
        return x
    return (int(x / step)) * step


# ---- per-asset overrides from env ----
def _parse_overrides(envvar: str) -> dict:
    raw = (os.getenv(envvar, "") or "").strip()
    out = {}
    if not raw:
        return out
    for item in raw.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            k = k.strip().upper()
            v = v.strip()
            try:
                out[k] = float(v)
            except Exception:
                pass
    return out


_SIZE_OVR = _parse_overrides("HYPER_SIZE_STEP_OVERRIDES")
_TICK_OVR = _parse_overrides("HYPER_PX_TICK_OVERRIDES")


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
            if (
                isinstance(v, (list, tuple))
                and v
                and isinstance(v[0], dict)
                and ("pxDecimals" in v[0] or "px_decimals" in v[0])
            ):
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
            c = _try_get_assets_container(info)
            if c and 0 <= res < len(c):
                return c[res]
    except Exception:
        pass
    c = _try_get_assets_container(info)
    if isinstance(c, (list, tuple)):
        for d in c:
            nm = d.get("name") or d.get("token") or d.get("symbol")
            if isinstance(nm, str) and nm.upper() == coin.upper():
                return d
    return None


def _get_asset_meta(info: Info, coin: str) -> tuple[float, float, Optional[float]]:
    price_tick = _FALLBACK_PRICE_TICK
    size_step = _FALLBACK_SIZE_STEP
    min_sz = _FALLBACK_MIN_SIZE

    asset = _resolve_asset_dict(info, coin)
    if asset:
        try:
            px_dec = int(asset.get("pxDecimals", asset.get("px_decimals", 2)))
            sz_dec = int(asset.get("szDecimals", asset.get("sz_decimals", 3)))
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

    cu = coin.upper()
    if cu in _SIZE_OVR:
        size_step = _SIZE_OVR[cu]
    if cu in _TICK_OVR:
        price_tick = _TICK_OVR[cu]

    log.info(
        "[HL] META coin=%s price_tick=%g size_step=%g min_sz=%s",
        coin,
        price_tick,
        size_step,
        "None" if min_sz is None else f"{min_sz}",
    )
    return price_tick, size_step, min_sz


# ---------- Idempotency ----------
def _redis_claim(client_id: str) -> Optional[bool]:
    if not _redis:
        return None
    try:
        ok = _redis.set(f"hl:idemp:{client_id}", "1", nx=True, ex=_IDEMP_TTL_SECS)
        if ok:
            log.info("[HL] IDEMP[redis]: claimed %s", client_id)
            return True
        log.info("[HL] SKIP duplicate (redis) %s", client_id)
        return False
    except Exception as e:
        log.exception("[HL] Redis idempotency error (falling back): %s", e)
        return None


def _sqlite_conn():
    conn = sqlite3.connect(_IDEMP_DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sent_client_ids (client_id TEXT PRIMARY KEY, ts INTEGER NOT NULL)"
    )
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
        conn.execute("DELETE FROM sent_client_ids WHERE ts<?", (now - _IDEMP_TTL_SECS,))
        conn.execute(
            "INSERT INTO sent_client_ids (client_id,ts) VALUES (?,?)", (client_id, now)
        )
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
            conn and conn.close()
        except Exception:
            pass
        if _filelock_supported and lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
            except Exception:
                pass


def _claim_client_id(client_id: Optional[str]) -> bool:
    if not client_id:
        return True
    if client_id in _SENT_CLIENT_IDS:
        log.info("[HL] SKIP duplicate (process) %s", client_id)
        return False
    r = _redis_claim(client_id)
    if r is True:
        _SENT_CLIENT_IDS.add(client_id)
        return True
    if r is False:
        return False
    if _sqlite_claim(client_id):
        _SENT_CLIENT_IDS.add(client_id)
        return True
    return False


# ---------- Open order dedupe ----------
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
                                if isinstance(x, dict):
                                    yield x
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
        for k in ("openOrders", "open_orders", "orders"):
            arr = state.get(k)
            if isinstance(arr, (list, tuple)):
                for x in arr:
                    if isinstance(x, dict):
                        yield x


def _order_matches(o: dict, coin: str, is_buy: bool, limit_px: float, size: float) -> bool:
    try:
        oc = o.get("coin") or o.get("asset") or o.get("symbol")
        ib = o.get("isBuy") if "isBuy" in o else o.get("is_buy")
        px = float(o.get("px", o.get("price")))
        sz = float(o.get("sz", o.get("size")))
        if isinstance(ib, str):
            ib = ib.lower() in ("true", "1", "yes", "buy", "long")
        return (
            (oc or "").upper() == coin.upper()
            and bool(ib) == bool(is_buy)
            and abs(px - float(limit_px)) <= 1e-12
            and abs(sz - float(size)) <= 1e-9
        )
    except Exception:
        return False


# ---------- Position helpers & TP/SL ----------
def _get_position_size(info: Info, coin: str) -> float:
    """Return signed position size (positive long, negative short), or 0."""
    try:
        state = None
        for attr in ("user_state", "userState", "account_state", "accountState"):
            fn = getattr(info, attr, None)
            if callable(fn):
                state = fn()
                break
        poss = None
        for key in ("positions", "openPositions", "pos", "open_pos"):
            if isinstance(state, dict) and key in state:
                poss = state[key]
                break
        if isinstance(poss, (list, tuple)):
            for p in poss:
                try:
                    name = (p.get("asset") or p.get("coin") or p.get("symbol") or "").upper()
                    if name == coin.upper():
                        return float(p.get("szi", p.get("sz", 0.0)))
                except Exception:
                    pass
    except Exception:
        pass
    return 0.0


def _split_sizes(total: float, n: int, mode: str = "equal", ratio: list[float] | None = None) -> list[float]:
    if n <= 1:
        return [total]
    if mode == "ratio" and ratio:
        s = sum(ratio) or 1.0
        parts = [total * (r / s) for r in ratio]
    else:
        q = int(total * 1e12 / n) / 1e12
        parts = [q] * (n - 1) + [max(total - q * (n - 1), 0.0)]
    return [x for x in parts if x > 0]


def _place_tp_orders(ex: Exchange, coin: str, is_buy_entry: bool, sizes: list[float], tp_prices: list[float]) -> None:
    orders = []
    for sz, px in zip(sizes, tp_prices):
        if px is None or sz <= 0:
            continue
        orders.append({
            "coin": coin,
            "is_buy": (not is_buy_entry),  # close the entry
            "sz": float(sz),
            "limit_px": float(px),
            "order_type": {"limit": {"tif": "Gtc"}},
            "reduce_only": True,
            "client_id": None,
        })
    if orders:
        log.info("[HL] SEND TP reduce-only orders: %s", orders)
        try:
            resp = ex.bulk_orders(orders)
            log.info("[HL] TP bulk_orders resp: %s", resp)
        except Exception as e:
            log.exception("[HL] ERROR sending TP orders: %s", e)


def _place_sl_order(ex: Exchange, coin: str, is_buy_entry: bool, sz: float, sl_px: float) -> None:
    """Try to place reduce-only STOP-MARKET via available trigger API."""
    if not (sl_px and sz > 0):
        return
    payload = {
        "coin": coin,
        "is_buy": (not is_buy_entry),  # closing side
        "sz": float(sz),
        "trigger": {
            "trigger_px": float(sl_px),
            "is_market": True,
            "tpsl": True,
        },
        "reduce_only": True,
        "client_id": None,
    }
    for meth in ("trigger_order", "trigger_orders", "batch_trigger_orders", "place_trigger_order"):
        fn = getattr(ex, meth, None)
        if not callable(fn):
            continue
        try:
            log.info("[HL] SEND SL reduce-only stop-market via %s: %s", meth, payload)
            resp = fn([payload]) if "s" in meth else fn(payload)
            log.info("[HL] SL trigger resp: %s", resp)
            return
        except Exception as e:
            log.warning("[HL] %s failed (will try next): %s", meth, e)
    log.warning("[HL] STOP could not be placed (no trigger API). Manage SL manually or upgrade SDK.")


# ---------- Main submit ----------
def submit_signal(sig) -> None:
    if sig is None:
        raise ValueError("submit_signal(sig): sig is None")
    entry_low = getattr(sig, "entry_low", None)
    entry_high = getattr(sig, "entry_high", None)
    if entry_low is None or entry_high is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    symbol = (getattr(sig, "symbol", "") or "")
    if not _symbol_ok(symbol):
        log.info("[HL] SKIP: %s not in HYPER_ONLY_EXECUTE_SYMBOLS=%s", symbol, sorted(_ALLOWED))
        return

    side_raw = (getattr(sig, "side", "") or "").upper()
    if side_raw not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported side '{side_raw}'.")
    side = "BUY" if side_raw == "LONG" else "SELL"

    coin = _coin_from_symbol(symbol)
    entry_low = float(entry_low)
    entry_high = float(entry_high)
    mid = (entry_low + entry_high) / 2.0
    client_id = getattr(sig, "client_id", None)

    if not _claim_client_id(client_id):
        log.info("[HL] SKIP: client_id already processed: %s", client_id)
        return

    ex, info = _mk_clients()
    price_tick, size_step, min_sz = _get_asset_meta(info, coin)

    limit_px = _quantize_down(mid, price_tick)
    tif = getattr(sig, "tif", None) or (_DEFAULT_TIF if _DEFAULT_TIF else None)
    tif_map = _order_type_for_tif(tif)
    if tif_map.get("limit", {}).get("tif") == "Alo":
        if side == "BUY":
            limit_px = _quantize_down(max(price_tick, limit_px - price_tick), price_tick)
        else:
            limit_px = _quantize_down(limit_px + price_tick, price_tick)
        log.info("[HL] ALO nudge applied: limit_px=%s tick=%s side=%s", limit_px, price_tick, side)

    if _FIXED_QTY is not None:
        raw_size = _FIXED_QTY
    else:
        notional = float(getattr(sig, "notional_usd", None) or _DEFAULT_NOTIONAL)
        raw_size = (notional / limit_px) if limit_px > 0 else 0.0
    size = _quantize_down(raw_size, size_step)

    min_floor = min_sz if min_sz is not None else (_FALLBACK_MIN_SIZE if _FALLBACK_MIN_SIZE is not None else 0.0)
    if size < min_floor and raw_size >= (min_floor if min_floor > 0 else _FALLBACK_SIZE_STEP):
        size = max(min_floor, _FALLBACK_SIZE_STEP)

    if min_sz is not None and size < min_sz:
        log.info(
            "[HL] SKIP: size %.10f < min %.10f for %s (raw=%.10f step=%g px=%.6f notional/qty=%s)",
            size,
            min_sz,
            coin,
            raw_size,
            size_step,
            limit_px,
            "fixed" if _FIXED_QTY is not None else "notional",
        )
        return
    if size <= 0.0:
        log.info(
            "[HL] SKIP: non-positive size %.10f (raw=%.10f step=%g coin=%s px=%.6f)",
            size,
            raw_size,
            size_step,
            coin,
            limit_px,
        )
        return

    is_buy = (side == "BUY")
    try:
        for o in _iter_open_orders(info):
            if _order_matches(o, coin, is_buy, limit_px, size):
                log.info("[HL] SKIP: identical open order already exists on book: %s", o)
                return
    except Exception as e:
        log.warning("[HL] open-order duplicate check failed (continuing): %s", e)

    log.info(
        "[HL] PLAN side=%s symbol=%s coin=%s band=(%.6f, %.6f) mid=%.6f pxTick=%g szStep=%g minSz=%s sz=%.10f SL=%s lev=%s TIF=%s client_id=%s",
        side,
        symbol,
        coin,
        entry_low,
        entry_high,
        mid,
        price_tick,
        size_step,
        "None" if min_sz is None else f"{min_sz}",
        size,
        getattr(sig, "stop_loss", None),
        getattr(sig, "leverage", None),
        tif_map,
        client_id,
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
    except Exception as e:
        log.exception("[HL] ERROR sending bulk_orders: %s", e)
        return

    # ---------------- TP/SL placement (optional) ----------------
    if _PLACE_TPSL:
        try:
            # Size to protect: if position already exists (filled), use it; else use entry order size
            try_pos_sz = abs(_get_position_size(info, coin))
            protect_sz = try_pos_sz if try_pos_sz > 0 else float(size)

            # STOP LOSS from signal
            sl_px = None
            if hasattr(sig, "stop_loss") and sig.stop_loss is not None:
                try:
                    sl_px = float(sig.stop_loss)
                except Exception:
                    sl_px = None

            # TAKE PROFIT targets: from signal.targets or env fallback
            tp_prices: list[float] = []
            tps = getattr(sig, "targets", None)
            if isinstance(tps, (list, tuple)) and tps:
                try:
                    tp_prices = [float(x) for x in tps if x is not None]
                except Exception:
                    tp_prices = []
            if not tp_prices and _DEFAULT_TP_PXS_RAW:
                try:
                    tp_prices = [float(x) for x in _DEFAULT_TP_PXS_RAW.split(",") if x.strip()]
                except Exception:
                    tp_prices = []

            # Place SL first (reduce-only stop-market) if we have one
            if sl_px:
                _place_sl_order(ex, coin, is_buy, protect_sz, sl_px)

            # Place TPs (reduce-only GTC limits) if provided
            if tp_prices:
                ratio: list[float] | None = None
                if _TP_SPLIT_MODE == "ratio" and _TP_SPLIT_RATIO_RAW:
                    try:
                        ratio = [float(x) for x in _TP_SPLIT_RATIO_RAW.split(",") if x.strip()]
                    except Exception:
                        ratio = None
                sizes = _split_sizes(protect_sz, len(tp_prices), _TP_SPLIT_MODE, ratio)
                _place_tp_orders(ex, coin, is_buy, sizes, tp_prices)
        except Exception as e:
            log.warning("[HL] TP/SL placement path failed (continuing): %s", e)

