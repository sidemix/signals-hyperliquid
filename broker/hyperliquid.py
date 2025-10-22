# broker/hyperliquid.py

from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, Optional, Set, Tuple, List

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

try:
    from eth_account import Account  # for banner
except Exception:
    Account = None  # type: ignore

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


# ========= Env helpers =========
def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None else str(v).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


# ========= Config =========
DRY_RUN = _env_bool("HYPER_DRY_RUN", _env_bool("DRY_RUN", True))
NETWORK = _env_str("HYPER_NETWORK", "mainnet").lower()
NOTIONAL_USD = _env_float("HYPER_NOTIONAL_USD", 50.0)

TIF = _env_str("HYPER_TIF", "Gtc").capitalize()
if TIF not in ("Gtc", "Ioc", "Alo"):
    log.warning(f"[WARN] HYPER_TIF={TIF} invalid; defaulting to Gtc")
    TIF = "Gtc"

AGENT_PRIVATE_KEY = _env_str("HYPER_AGENT_PRIVATE_KEY", "")
VAULT_ADDRESS = _env_str("HYPER_VAULT_ADDRESS", "").lower() or None

_only_exec = _env_str("HYPER_ONLY_EXECUTE_SYMBOLS", "*")
if _only_exec == "*":
    ONLY_EXECUTE_SYMBOLS: Optional[Set[str]] = None
else:
    ONLY_EXECUTE_SYMBOLS = {s.strip().upper() for s in _only_exec.split(",") if s.strip()}

BASE_URL = "https://api.hyperliquid-testnet.xyz" if NETWORK == "testnet" else "https://api.hyperliquid.xyz"

_info: Optional[Info] = None
_ex: Optional[Exchange] = None


def _get_info() -> Info:
    global _info
    if _info is None:
        _info = Info(base_url=BASE_URL)
    return _info


def _get_exchange() -> Exchange:
    global _ex
    if _ex is None:
        if not AGENT_PRIVATE_KEY and not DRY_RUN:
            raise RuntimeError("HYPER_AGENT_PRIVATE_KEY is required when HYPER_DRY_RUN=false")
        _ex = Exchange(AGENT_PRIVATE_KEY or "0x" + "0" * 64, base_url=BASE_URL)
    return _ex


def _print_mode_banner() -> None:
    mode = "DRY_RUN" if DRY_RUN else "LIVE"
    agent_addr = None
    if Account and AGENT_PRIVATE_KEY:
        try:
            agent_addr = Account.from_key(AGENT_PRIVATE_KEY).address
        except Exception:
            agent_addr = None
    if agent_addr:
        print(f"[MODE] {mode} with agent={agent_addr}{' (vault=' + VAULT_ADDRESS + ')' if VAULT_ADDRESS else ''}")
    else:
        print(f"[MODE] {mode}{' (no agent key loaded)' if not AGENT_PRIVATE_KEY else ''}")


_print_mode_banner()


# ========= Utils =========
_ASSET_CACHE: Dict[str, int] = {}
_UID_SEEN: Set[str] = set()


def _symbol_to_coin(symbol: str) -> str:
    s = symbol.strip().upper()
    return s.split("/")[0] if "/" in s else s


def _coin_to_asset_idx(coin: str) -> int:
    coin_u = coin.upper()
    if coin_u in _ASSET_CACHE:
        return _ASSET_CACHE[coin_u]
    info = _get_info()
    meta = info.meta()
    universe = meta.get("universe", [])
    for idx, entry in enumerate(universe):
        name = str(entry.get("name", "")).upper()
        if name == coin_u:
            _ASSET_CACHE[coin_u] = idx
            return idx
    raise RuntimeError(f"Asset index not found for coin={coin_u}")


def _get_mark_price(coin: str, fallback_px: Optional[float] = None) -> Optional[float]:
    info = _get_info()
    try:
        mids = info.all_mids()
        if isinstance(mids, dict):
            inner = mids.get("mids", mids)
            val = inner.get(coin.upper())
            if val is not None:
                return float(val)
    except Exception as e:
        log.warning(f"[WARN] all_mids failed for {coin}: {e}")
    try:
        trades = info.recent_trades(coin.upper(), n=1)
        if isinstance(trades, list) and trades:
            px = trades[0].get("px")
            if px is not None:
                return float(px)
    except Exception as e:
        log.warning(f"[WARN] recent_trades failed for {coin}: {e}")
    return fallback_px


def _compute_size_from_notional(notional_usd: float, px: Optional[float]) -> Optional[float]:
    if px is None or px <= 0:
        return None
    return notional_usd / px


# ========= Signal extraction =========
def _extract_signal(sig: Any) -> Dict[str, Any]:
    def g(name: str, *alts: str, default: Any = None) -> Any:
        names = (name,) + alts
        for n in names:
            if hasattr(sig, n):
                return getattr(sig, n)
            if isinstance(sig, dict) and n in sig:
                return sig[n]
        return default

    side = g("side")
    symbol = g("symbol") or g("pair") or g("market")
    band_low = g("band_low", "entry_low", "entry_band_low", "bandMin", default=None)
    band_high = g("band_high", "entry_high", "entry_band_high", "bandMax", default=None)
    if band_low is None or band_high is None:
        band = g("band", "entry_band", default=None)
        if band and isinstance(band, (tuple, list)) and len(band) == 2:
            band_low, band_high = band
    stop_loss = g("stop_loss", "sl", "SL", default=None)
    lev = g("lev", "leverage", default=None)
    uid = g("uid", "message_uid", "id", default=None)

    if side is None or symbol is None or band_low is None or band_high is None:
        raise ValueError("Signal missing side/symbol/band_low/band_high.")

    return {
        "side": str(side),
        "symbol": str(symbol),
        "band_low": float(band_low),
        "band_high": float(band_high),
        "stop_loss": float(stop_loss) if stop_loss is not None else None,
        "lev": float(lev) if lev is not None else None,
        "uid": str(uid) if uid is not None else None,
    }


# ========= Order placement (adapts to SDK) =========
def _place_order_real(
    *,
    coin: str,
    asset_idx: int | None = None,
    asset: int | None = None,
    side: str | None = None,
    is_buy: bool | None = None,
    px: str | None = None,
    px_str: str | None = None,
    sz: str | None = None,
    sz_str: str | None = None,
    size_str: str | None = None,
    tif: str = "Gtc",
    reduce_only: bool,
) -> Dict[str, Any]:
    """
    Tries multiple SDK signatures:

    A) NEW dict style:
         ex.order(action, nonce=..., vaultAddress=...)
    B) LEGACY dict wrapper:
         ex.order({"action":action, "nonce":...})
    C) POSITIONAL families (we try several):
         ex.order(is_buy, sz, limit_px, order_type, reduce_only, asset)
         ex.order(is_buy, sz, limit_px, order_type)
         ex.order(is_buy, sz, limit_px, "limit", tif, reduce_only, asset)
         ex.order(is_buy, sz, limit_px, "limit", tif)
         ex.order(is_buy, sz, limit_px, tif, reduce_only, asset)
         ex.order(is_buy, sz, limit_px, tif)
    with both float and str for px/sz.
    """

    a = asset_idx if asset_idx is not None else asset
    if a is None:
        raise ValueError("asset index is required")

    if is_buy is None:
        if side is None:
            raise ValueError("either `side` or `is_buy` must be provided")
        side_u = side.upper()
        is_buy = side_u in ("BUY", "LONG")

    px_val = px_str if px_str is not None else px
    sz_val = sz_str if sz_str is not None else (size_str if size_str is not None else sz)
    if px_val is None or sz_val is None:
        raise ValueError("both price and size are required (px/px_str, sz/sz_str/size_str)")

    # normalize as str (dict-based) and float (positional-based)
    px_s = str(px_val)
    sz_s = str(sz_val)
    try:
        px_f = float(px_s)
    except Exception:
        px_f = None
    try:
        sz_f = float(sz_s)
    except Exception:
        sz_f = None

    nonce = int(time.time() * 1000)

    order_type_dict = {"limit": {"tif": tif}}
    order_type_str = "limit"

    action = {
        "type": "order",
        "orders": [{
            "a": a,
            "b": bool(is_buy),
            "p": px_s,
            "s": sz_s,
            "r": reduce_only,
            "t": order_type_dict,
        }],
        "grouping": "na",
    }

    ex = _get_exchange()

    # A) New dict style
    try:
        resp = ex.order(action, nonce=nonce, vaultAddress=VAULT_ADDRESS or None)  # type: ignore[arg-type]
        log.info(f"[BROKER] order response (new): {resp}")
        if isinstance(resp, dict) and resp.get("status") == "ok":
            return resp
        raise RuntimeError(f"Order rejected by API: {resp}")
    except TypeError:
        pass
    except Exception as e:
        log.warning(f"[WARN] new-style order failed: {e}")

    # B) Legacy dict wrapper
    try:
        payload = {"action": action, "nonce": nonce}
        if VAULT_ADDRESS:
            payload["vaultAddress"] = VAULT_ADDRESS
        resp = ex.order(payload)
        log.info(f"[BROKER] order response (legacy-dict): {resp}")
        if isinstance(resp, dict) and resp.get("status") == "ok":
            return resp
        raise RuntimeError(f"Order rejected by API: {resp}")
    except TypeError:
        pass
    except Exception as e:
        log.warning(f"[WARN] legacy-dict order failed: {e}")

    # C) Positional families (try a lot, noisy on purpose so we can see which wins)
    attempts: List[Tuple[str, Tuple[Any, ...]]] = []

    # With dict order_type, prefer floats when available
    if sz_f is not None and px_f is not None:
        attempts += [
            ("pos-6-float", (bool(is_buy), sz_f, px_f, order_type_dict, reduce_only, a)),
            ("pos-4-float", (bool(is_buy), sz_f, px_f, order_type_dict)),
        ]
    # With dict order_type, strings
    attempts += [
        ("pos-6-str", (bool(is_buy), sz_s, px_s, order_type_dict, reduce_only, a)),
        ("pos-4-str", (bool(is_buy), sz_s, px_s, order_type_dict)),
    ]

    # With string "limit" + separate tif
    if sz_f is not None and px_f is not None:
        attempts += [
            ("pos-limit-tif-7-float", (bool(is_buy), sz_f, px_f, order_type_str, tif, reduce_only, a)),
            ("pos-limit-tif-5-float", (bool(is_buy), sz_f, px_f, order_type_str, tif)),
        ]
    attempts += [
        ("pos-limit-tif-7-str", (bool(is_buy), sz_s, px_s, order_type_str, tif, reduce_only, a)),
        ("pos-limit-tif-5-str", (bool(is_buy), sz_s, px_s, order_type_str, tif)),
    ]

    # With bare tif (some variants use tif as 4th arg)
    if sz_f is not None and px_f is not None:
        attempts += [
            ("pos-tif-6-float", (bool(is_buy), sz_f, px_f, tif, reduce_only, a)),
            ("pos-tif-4-float", (bool(is_buy), sz_f, px_f, tif)),
        ]
    attempts += [
        ("pos-tif-6-str", (bool(is_buy), sz_s, px_s, tif, reduce_only, a)),
        ("pos-tif-4-str", (bool(is_buy), sz_s, px_s, tif)),
    ]

    last_err: Optional[Exception] = None
    for label, args in attempts:
        try:
            resp = ex.order(*args)  # type: ignore[misc]
            log.info(f"[BROKER] order response ({label}): {resp}")
            # If dict-like, check status
            if isinstance(resp, dict):
                if resp.get("status") == "ok":
                    return resp
                raise RuntimeError(f"Order rejected by API: {resp}")
            # Non-dict truthy → consider success
            return {"status": "ok", "response": resp, "via": label}
        except TypeError as e:
            log.warning(f"[WARN] {label} signature failed: {e}")
            last_err = e
        except Exception as e:
            log.warning(f"[WARN] {label} order failed: {e}")
            last_err = e

    raise RuntimeError(f"All SDK order call styles failed. Last error: {last_err}")


# ========= Public entry =========
def submit_signal(sig: Any) -> Optional[Dict[str, Any]]:
    s = _extract_signal(sig)
    side = s["side"].upper()
    symbol = s["symbol"].upper()
    band_low = s["band_low"]
    band_high = s["band_high"]
    stop_loss = s["stop_loss"]
    uid = s["uid"]

    if uid and uid in _UID_SEEN:
        log.info(f"[SKIP] duplicate uid={uid}")
        return None
    if uid:
        _UID_SEEN.add(uid)

    coin = _symbol_to_coin(symbol)

    if ONLY_EXECUTE_SYMBOLS is not None and symbol not in ONLY_EXECUTE_SYMBOLS:
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}")
        return None

    asset = _coin_to_asset_idx(coin)

    if side in ("BUY", "LONG"):
        px_entry = float(band_low)
        is_buy = True
    else:
        px_entry = float(band_high)
        is_buy = False

    mark = _get_mark_price(coin, fallback_px=px_entry)
    if mark is None:
        log.warning(f"[PRICE] mark fetch failed for {coin}; falling back to entry price for sizing")

    sz_float = _compute_size_from_notional(NOTIONAL_USD, mark if mark is not None else px_entry)
    if sz_float is None or sz_float <= 0:
        raise RuntimeError("Could not compute size from mark price; aborting.")

    px_entry_str = f"{px_entry:.8f}".rstrip("0").rstrip(".")
    sz_str = f"{sz_float:.8f}".rstrip("0").rstrip(".")

    log.info(f"[BROKER] {side} {symbol} band=({band_low:.6f},{band_high:.6f})"
             f"{' SL=' + str(stop_loss) if stop_loss is not None else ''} lev={s.get('lev') if s.get('lev') else ''} TIF={TIF}")
    log.info("Websocket connected")
    if mark is not None:
        log.info(f"[PRICE] {coin} mark={mark:.5f}")

    log.info(f"[PLAN] side={'BUY' if is_buy else 'SELL'} coin={coin} a={asset} px={px_entry_str} "
             f"sz={sz_str} tif={TIF} reduceOnly=False")

    if DRY_RUN:
        print(f"[DRYRUN] submit LIMIT {'BUY' if is_buy else 'SELL'} {coin} a={asset} px={px_entry_str} sz={sz_str} tif={TIF}")
        return {
            "status": "dryrun",
            "plan": {
                "asset": asset,
                "coin": coin,
                "isBuy": is_buy,
                "px": px_entry_str,
                "sz": sz_str,
                "tif": TIF,
                "reduceOnly": False,
            },
        }

    resp = _place_order_real(
        coin=coin,
        asset_idx=asset,
        is_buy=is_buy,
        px_str=px_entry_str,
        sz_str=sz_str,
        tif=TIF,
        reduce_only=False,
    )
    return resp
