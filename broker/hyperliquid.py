# broker/hyperliquid.py

from __future__ import annotations

import os
import time
import logging
from typing import Any, Dict, Optional, Tuple, List, Set

# HL SDK (0.20.x compatible, and newer where possible)
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange

# Nice-to-have for banner
try:
    from eth_account import Account  # type: ignore
except Exception:  # pragma: no cover
    Account = None  # type: ignore

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


# ========== Env helpers ==========
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


# ========== Config from env ==========
# DRY-RUN: prefer HYPER_DRY_RUN, fallback to DRY_RUN, default True (safe)
DRY_RUN = _env_bool("HYPER_DRY_RUN", _env_bool("DRY_RUN", True))

NETWORK = _env_str("HYPER_NETWORK", "mainnet").lower()  # "mainnet" | "testnet"

# Notional USD sizing target per entry
NOTIONAL_USD = _env_float("HYPER_NOTIONAL_USD", 50.0)

# Time-in-force: Gtc | Ioc | Alo  (case-insensitive in env; normalized to Title case)
TIF = _env_str("HYPER_TIF", "Gtc").capitalize()
if TIF not in ("Gtc", "Ioc", "Alo"):
    log.warning(f"[WARN] HYPER_TIF={TIF} invalid; defaulting to Gtc")
    TIF = "Gtc"

# Approved API wallet (Agent wallet) private key
AGENT_PRIVATE_KEY = _env_str("HYPER_AGENT_PRIVATE_KEY", "")

# Optional subaccount/vault address (0x..)
VAULT_ADDRESS = _env_str("HYPER_VAULT_ADDRESS", "").lower() or None

# Symbol allowlist (comma-separated) or "*" for all
_only_exec = _env_str("HYPER_ONLY_EXECUTE_SYMBOLS", "*")
ONLY_EXECUTE_SYMBOLS: Optional[Set[str]]
if _only_exec == "*":
    ONLY_EXECUTE_SYMBOLS = None
else:
    ONLY_EXECUTE_SYMBOLS = {s.strip().upper() for s in _only_exec.split(",") if s.strip()}

# HL endpoints
if NETWORK == "testnet":
    BASE_URL = "https://api.hyperliquid-testnet.xyz"
else:
    BASE_URL = "https://api.hyperliquid.xyz"

# ========== Clients ==========
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
            raise RuntimeError(
                "HYPER_AGENT_PRIVATE_KEY is required when HYPER_DRY_RUN=false"
            )
        # Exchange(private_key, base_url)
        _ex = Exchange(AGENT_PRIVATE_KEY or "0x" + "0" * 64, base_url=BASE_URL)  # dummy key ok in DRY_RUN
    return _ex


# ========== Startup banner ==========
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


# ========== Utilities ==========
_ASSET_CACHE: Dict[str, int] = {}          # coin -> asset index
_UID_SEEN: Set[str] = set()                # simple duplicate guard


def _symbol_to_coin(symbol: str) -> str:
    """
    Convert "ETH/USD" -> "ETH" ; also uppercase & trim.
    """
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/")[0]
    return s


def _coin_to_asset_idx(coin: str) -> int:
    """
    Map coin symbol to asset index using meta.universe.
    Cache results for speed.
    """
    coin_u = coin.upper()
    if coin_u in _ASSET_CACHE:
        return _ASSET_CACHE[coin_u]

    info = _get_info()
    # 0.20.x Info has .meta() returning dict with "universe": [{"name": "BTC", ...}, ...]
    meta = info.meta()
    universe = meta.get("universe", [])
    for idx, entry in enumerate(universe):
        name = str(entry.get("name", "")).upper()
        if name == coin_u:
            _ASSET_CACHE[coin_u] = idx
            return idx
    raise RuntimeError(f"Asset index not found for coin={coin_u}")


def _get_mark_price(coin: str, fallback_px: Optional[float] = None) -> Optional[float]:
    """
    Try to fetch a reasonable mark/mid price for sizing.
    Strategy:
      1) allMids
      2) lastTrade
      3) fallback_px (if provided)
    """
    info = _get_info()
    # 1) allMids
    try:
        mids = info.all_mids()  # {"mids": {"ETH": "3000.12", ...}} or {"ETH":"3000.12"} depending on SDK
        if isinstance(mids, dict):
            inner = mids.get("mids", mids)  # handle either shape
            val = inner.get(coin.upper())
            if val is not None:
                return float(val)
    except Exception as e:
        log.warning(f"[WARN] all_mids failed for {coin}: {e}")

    # 2) last trade price
    try:
        trades = info.recent_trades(coin.upper(), n=1)  # [{"px": "....", ...}]
        if isinstance(trades, list) and trades:
            px = trades[0].get("px")
            if px is not None:
                return float(px)
    except Exception as e:
        log.warning(f"[WARN] recent_trades failed for {coin}: {e}")

    # 3) fallback
    if fallback_px is not None:
        return fallback_px
    return None


def _compute_size_from_notional(notional_usd: float, px: Optional[float]) -> Optional[float]:
    if px is None or px <= 0:
        return None
    return notional_usd / px


# ========== Signal extraction ==========
def _extract_signal(sig: Any) -> Dict[str, Any]:
    """
    Normalize the ExecSignal-like object into dict fields we use:
      side  : "LONG"/"SHORT" or "BUY"/"SELL"
      symbol: "ETH/USD" etc
      band_low, band_high : floats (entry band)
      stop_loss           : float (optional)
      lev                 : float (optional leverage)
      uid                 : idempotency key (optional)
    Accepts either attributes or dict-like keys.
    """
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
    # Entry band synonyms
    band_low = g("band_low", "entry_low", "entry_band_low", "bandMin", default=None)
    band_high = g("band_high", "entry_high", "entry_band_high", "bandMax", default=None)
    # Sometimes stored as a tuple or "band"
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


# ========== Order placement (SDK-compatible) ==========
def _place_order_real(
    *,
    coin: str,
    # asset synonyms
    asset_idx: int | None = None,
    asset: int | None = None,
    # side synonyms
    side: str | None = None,
    is_buy: bool | None = None,
    # price/size synonyms
    px: str | None = None,
    px_str: str | None = None,
    sz: str | None = None,
    sz_str: str | None = None,
    size_str: str | None = None,
    tif: str = "Gtc",
    reduce_only: bool,
) -> Dict[str, Any]:
    """
    Place a real order via the HL SDK.

    Accepts either:
      - asset_idx or asset
      - side ("BUY"/"SELL"/"LONG"/"SHORT") or is_buy (bool)
      - px or px_str
      - sz or sz_str or size_str

    Supports both SDK call shapes:
      - NEWER:  ex.order(action, nonce=..., vaultAddress=...)
      - LEGACY: ex.order({"action": action, "nonce": ..., "vaultAddress": ...})
    """
    a = asset_idx if asset_idx is not None else asset
    if a is None:
        raise ValueError("asset index is required")

    # normalize side
    if is_buy is None:
        if side is None:
            raise ValueError("either `side` or `is_buy` must be provided")
        side_u = side.upper()
        is_buy = side_u in ("BUY", "LONG")

    # normalize px/sz
    px_val = px_str if px_str is not None else px
    sz_val = sz_str if sz_str is not None else (size_str if size_str is not None else sz)
    if px_val is None or sz_val is None:
        raise ValueError("both price and size are required (px/px_str, sz/sz_str/size_str)")

    px_val = str(px_val)
    sz_val = str(sz_val)

    nonce = int(time.time() * 1000)

    action = {
        "type": "order",
        "orders": [{
            "a": a,
            "b": bool(is_buy),
            "p": px_val,
            "s": sz_val,
            "r": reduce_only,
            "t": {"limit": {"tif": tif}},
        }],
        "grouping": "na",
    }

    ex = _get_exchange()
    try:
        # Try newer SDK signature
        resp = ex.order(action, nonce=nonce, vaultAddress=VAULT_ADDRESS or None)  # type: ignore[arg-type]
    except TypeError:
        # Fall back to legacy payload dict (0.20.x)
        payload = {"action": action, "nonce": nonce}
        if VAULT_ADDRESS:
            payload["vaultAddress"] = VAULT_ADDRESS
        resp = ex.order(payload)

    log.info(f"[BROKER] order response: {resp}")
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        raise RuntimeError(f"Order rejected by API: {resp}")
    return resp


# ========== Public entry point ==========
def submit_signal(sig: Any) -> Optional[Dict[str, Any]]:
    """
    Bridge function called by execution.py. Parses the signal,
    computes a plan, then places (or DRY-RUN prints) the order.
    """
    s = _extract_signal(sig)
    side = s["side"].upper()
    symbol = s["symbol"].upper()
    band_low = s["band_low"]
    band_high = s["band_high"]
    stop_loss = s["stop_loss"]
    uid = s["uid"]

    # Duplicate guard (idempotency)
    if uid and uid in _UID_SEEN:
        log.info(f"[SKIP] duplicate uid={uid}")
        return None
    if uid:
        _UID_SEEN.add(uid)

    # Map symbol -> coin
    coin = _symbol_to_coin(symbol)

    # Filter by ONLY_EXECUTE_SYMBOLS
    if ONLY_EXECUTE_SYMBOLS is not None and symbol not in ONLY_EXECUTE_SYMBOLS:
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {symbol}")
        return None

    # Asset index
    asset = _coin_to_asset_idx(coin)

    # Entry price = band edge (buyer prefers lower edge; seller prefers upper edge)
    if side in ("BUY", "LONG"):
        px_entry = float(band_low)
        is_buy = True
    else:
        px_entry = float(band_high)
        is_buy = False

    # Mark price for sizing; fallback to entry px if unavailable
    mark = _get_mark_price(coin, fallback_px=px_entry)
    if mark is None:
        log.warning(f"[PRICE] mark fetch failed for {coin}; falling back to entry price for sizing")

    # Compute size from notional
    sz_float = _compute_size_from_notional(NOTIONAL_USD, mark if mark is not None else px_entry)
    if sz_float is None or sz_float <= 0:
        raise RuntimeError("Could not compute size from mark price; aborting.")

    # Convert to strings for SDK (let HL enforce tick/lot)
    px_entry_str = f"{px_entry:.8f}".rstrip("0").rstrip(".")
    sz_str = f"{sz_float:.8f}".rstrip("0").rstrip(".")

    log.info(f"[BROKER] {side} {symbol} band=({band_low:.6f},{band_high:.6f})"
             f"{' SL=' + str(stop_loss) if stop_loss is not None else ''} lev={s.get('lev') if s.get('lev') else ''} TIF={TIF}")
    log.info("Websocket connected")  # to mirror your existing logs
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

    # LIVE
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
