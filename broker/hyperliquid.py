# broker/hyperliquid.py
from __future__ import annotations

import os
import time
import math
import logging
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    h.setFormatter(fmt)
    log.addHandler(h)
log.setLevel(logging.INFO)

# -----------------------
# Environment & constants
# -----------------------

def _getenv_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _getenv_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "").strip() or default)
    except Exception:
        return default

HYPER_DRY_RUN = _getenv_bool("HYPER_DRY_RUN", False) or _getenv_bool("hyper_dry_run", False)
HL_NETWORK = (os.getenv("HL_NETWORK") or os.getenv("HYPER_NETWORK") or "mainnet").strip().lower()
USER_ADDRESS = (os.getenv("HL_ADDRESS") or os.getenv("HYPER_USER_ADDRESS") or "").strip() or None
VAULT_ADDRESS = (os.getenv("HL_VAULT_ADDRESS") or os.getenv("HYPER_VAULT_ADDRESS") or "").strip() or None

# Agent/API wallet private key (hex with 0x prefix)
AGENT_WALLET_PK = (
    os.getenv("HL_AGENT_WALLET_PK")
    or os.getenv("HL_API_WALLET_PK")
    or os.getenv("HYPER_AGENT_PK")
    or os.getenv("HYPER_API_WALLET_PK")
    or os.getenv("HL_PRIVATE_KEY")   # fallback if user stored here
    or os.getenv("HYPER_PRIVATE_KEY")
    or ""
).strip()

DEFAULT_TIF = (os.getenv("HYPER_DEFAULT_TIF") or "Gtc").strip()
ALLOWED = (os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS") or "").strip()
ALLOWED_SET = set([s.strip().upper() for s in ALLOWED.split(",") if s.strip()])

USD_PER_ORDER = _getenv_float("HYPER_ORDER_USD_AMT", 50.0)

# -----------
# SDK wiring
# -----------

_EXC: Optional[Any] = None
_INFO: Optional[Any] = None
_CHAIN = None

def _resolve_chain() -> Any:
    global _CHAIN
    if _CHAIN is not None:
        return _CHAIN
    try:
        from hyperliquid.exchange import Chain
    except Exception:
        # fallback name in some older builds
        class _C:
            MAINNET = "mainnet"
            TESTNET = "testnet"
        Chain = _C  # type: ignore
    if HL_NETWORK in ("testnet", "hl-testnet", "tn"):
        _CHAIN = getattr(__import__("hyperliquid.exchange", fromlist=["Chain"]), "Chain").TESTNET if hasattr(getattr(__import__("hyperliquid.exchange", fromlist=["Chain"]), "Chain"), "TESTNET") else "testnet"
    else:
        _CHAIN = getattr(__import__("hyperliquid.exchange", fromlist=["Chain"]), "Chain").MAINNET if hasattr(getattr(__import__("hyperliquid.exchange", fromlist=["Chain"]), "Chain"), "MAINNET") else "mainnet"
    return _CHAIN

def _get_info():
    """Construct (and cache) an Info client."""
    global _INFO
    if _INFO is not None:
        return _INFO
    chain = _resolve_chain()
    try:
        from hyperliquid.info import Info
        _INFO = Info(chain)
        return _INFO
    except TypeError:
        # Some SDKs accept a base_url instead of Chain
        try:
            from hyperliquid.info import Info
            base = "https://api.hyperliquid-testnet.xyz" if str(chain).lower().endswith("testnet") else "https://api.hyperliquid.xyz"
            _INFO = Info(base)
            return _INFO
        except Exception as e:
            raise RuntimeError(f"Unable to construct Info client: {e}") from e

def _get_exchange():
    """Construct (and cache) an Exchange client using the agent/API wallet PK."""
    global _EXC
    if _EXC is not None:
        return _EXC

    if not AGENT_WALLET_PK:
        raise RuntimeError("No agent/API wallet private key set. Populate HL_AGENT_WALLET_PK (or HL_PRIVATE_KEY).")

    chain = _resolve_chain()

    # Try a few constructor signatures
    from hyperliquid.exchange import Exchange
    exc: Optional[Any] = None
    err: Optional[Exception] = None

    # 1) Newer SDKs
    try:
        exc = Exchange(private_key=AGENT_WALLET_PK, chain=chain)  # type: ignore
        _EXC = exc
        return _EXC
    except Exception as e:
        err = e

    # 2) Some builds want 'key' instead of 'private_key'
    try:
        exc = Exchange(key=AGENT_WALLET_PK, chain=chain)  # type: ignore
        _EXC = exc
        return _EXC
    except Exception as e:
        err = e

    # 3) Some builds accept (private_key, account_address, chain)
    try:
        if USER_ADDRESS:
            exc = Exchange(private_key=AGENT_WALLET_PK, account_address=USER_ADDRESS, chain=chain)  # type: ignore
            _EXC = exc
            return _EXC
    except Exception as e:
        err = e

    raise RuntimeError(f"Unable to construct Exchange client with provided SDK: {err}")

# -------------------------
# Helpers: price & decimals
# -------------------------

def _get_asset_index(coin: str) -> int:
    """Resolve asset index for a perp coin symbol via Info."""
    info = _get_info()
    # SDK method name_to_asset(symbol) exists per your logs
    a = info.name_to_asset(coin)
    if a is None:
        raise RuntimeError(f"Unknown coin {coin}")
    return int(a)

def _get_mark_price(coin: str) -> Optional[float]:
    """Try a few ways to grab a mark (or mid) price for a coin."""
    info = _get_info()

    # 1) activeAssetCtx (preferred if available)
    try:
        if hasattr(info, "active_asset_ctx"):
            ctx = info.active_asset_ctx(coin)  # may raise on older builds
            if ctx and "ctx" in ctx and ctx["ctx"] and "markPx" in ctx["ctx"]:
                return float(ctx["ctx"]["markPx"])
    except Exception as e:
        log.warning(f"active_asset_ctx failed for {coin}: {e}")

    # 2) allMids
    try:
        if hasattr(info, "all_mids"):
            mids = info.all_mids()
            mid_s = mids.get(coin)
            if mid_s is not None:
                return float(mid_s)
    except Exception as e:
        log.warning(f"all_mids failed for {coin}: {e}")

    # 3) l2Book mid
    try:
        if hasattr(info, "l2_book"):
            book = info.l2_book(coin, nSigFigs=5, mantissa=None)
            if isinstance(book, dict) and "levels" in book:
                bids, asks = book["levels"]
                if bids and asks:
                    bid = float(bids[0]["px"])
                    ask = float(asks[0]["px"])
                    return (bid + ask) / 2.0
    except Exception as e:
        log.warning(f"l2_book mid failed for {coin}: {e}")

    return None

# --------------------------
# OrderType / TimeInForce
# --------------------------

def _mk_order_type(tif: str = "Gtc"):
    """
    Returns an SDK OrderType object for a limit order with the given TIF.
    Falls back to the dict shape if SDK types are not available.
    """
    tif_norm = (tif or "Gtc").strip().lower()
    tif_norm = {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}.get(tif_norm, "Gtc")

    try:
        from hyperliquid.utils.signing import OrderType, TimeInForce  # type: ignore
        tif_enum = getattr(TimeInForce, tif_norm)
        return OrderType.Limit(tif_enum)
    except Exception:
        # Dict fallback (useful for dict-based endpoints; won’t be used in positional call)
        return {"limit": {"tif": tif_norm}}

# --------------------------
# PUBLIC: submit API
# --------------------------

def _pick_entry_price(is_buy: bool, band_low: float, band_high: float) -> float:
    # Simple policy: buy at lower band edge, sell at upper band edge
    return band_low if is_buy else band_high

def _normalize_symbol(raw: str) -> Tuple[str, str]:
    """From 'ZRO/USD' -> ('ZRO', 'ZRO/USD')"""
    sym = raw.strip().upper().replace("USDT", "USD").replace("PERP", "").replace("-PERP", "")
    coin = sym.split("/")[0].strip()
    return coin, sym

def _round_sz_to_decimals(coin: str, sz: float) -> float:
    """Round size to the asset's szDecimals if available; otherwise return as-is."""
    try:
        info = _get_info()
        meta = getattr(info, "meta") if hasattr(info, "meta") else None
        if callable(meta):
            meta = meta()
        if meta and "universe" in meta:
            # meta['universe'] is list of assets; each has 'szDecimals'
            for a in meta["universe"]:
                if a.get("name") == coin and "szDecimals" in a:
                    d = int(a["szDecimals"])
                    factor = 10 ** d
                    return math.floor(sz * factor + 1e-9) / factor
    except Exception:
        pass
    return sz

def _place_order_real(
    *,
    coin: str,
    asset_idx: int | None = None,   # kept for compatibility, unused in positional SDK call
    asset: int | None = None,       # kept for compatibility, unused in positional SDK call
    side: str | None = None,
    is_buy: bool | None = None,
    px: str | None = None,
    px_str: str | None = None,
    sz: str | None = None,
    sz_str: str | None = None,
    size_str: str | None = None,
    tif: str = "Gtc",
    reduce_only: bool = False,
    cloid: str | None = None,
) -> Dict[str, Any]:
    """
    Places a LIMIT order using the SDK's positional signature discovered in logs:

      ex.order(
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: OrderType,
        reduce_only: bool = False,
        cloid: Optional[Cloid] = None,
        builder: Optional[BuilderInfo] = None
      )
    """
    ex = _get_exchange()

    # Decide side
    if is_buy is None:
        if not side:
            raise ValueError("either `side` or `is_buy` must be provided")
        is_buy = side.strip().upper() in ("BUY", "LONG")

    # Choose strings first, then coerce to floats
    px_val = px_str if px_str is not None else px
    sz_val = sz_str if sz_str is not None else (size_str if size_str is not None else sz)
    if px_val is None or sz_val is None:
        raise ValueError("both price and size are required (px/px_str, sz/sz_str/size_str)")

    px_s = str(px_val)
    sz_s = str(sz_val)

    try:
        limit_px = float(px_s)
        sz_f = float(sz_s)
    except Exception as e:
        raise ValueError(f"invalid numeric px/sz: px={px_s!r} sz={sz_s!r}; {e}")

    order_type = _mk_order_type(tif)

    if HYPER_DRY_RUN:
        log.info(f"[DRYRUN] submit LIMIT {'BUY' if is_buy else 'SELL'} {coin} px={limit_px} sz={sz_f} tif={tif} reduceOnly={reduce_only}")
        return {"status": "ok", "dryRun": True}

    # Positional SDK call per your signature
    try:
        args = (coin, bool(is_buy), sz_f, limit_px, order_type)
        kwargs: Dict[str, Any] = {"reduce_only": bool(reduce_only)}
        if cloid:
            kwargs["cloid"] = cloid
        resp = ex.order(*args, **kwargs)  # type: ignore[misc]
        log.info(f"[BROKER] order response (positional SDK): {resp}")

        if isinstance(resp, dict):
            if resp.get("status") == "ok":
                return resp
            if resp.get("success") is True:
                return {"status": "ok", "response": resp}
            raise RuntimeError(f"Order rejected: {resp}")
        if resp:
            return {"status": "ok", "response": resp}

        raise RuntimeError(f"Unexpected falsy response: {resp!r}")
    except Exception as e:
        raise RuntimeError(f"SDK order failed: {e}") from e

def submit_signal(sig) -> Dict[str, Any]:
    """
    Bridge layer called by execution.py. Expects `sig` to expose:
      - side ('LONG'/'SHORT' or 'BUY'/'SELL')
      - symbol (e.g. 'ZRO/USD')
      - band_low, band_high (floats)
      - stop_loss (float) (optional but logged)
      - leverage (float) (optional; we size purely by USD_PER_ORDER here)
      - timeframe (str)
    """
    # 1) Normalize and filter symbol
    coin, sym = _normalize_symbol(sig.symbol)
    if ALLOWED_SET and sym.upper() not in ALLOWED_SET and coin.upper() not in ALLOWED_SET:
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {sym}")
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": sym}

    # 2) Resolve asset index (for logging/consistency; not needed for positional call)
    try:
        a = _get_asset_index(coin)
    except Exception as e:
        return {"status": "error", "reason": f"asset index lookup failed: {e}"}

    # 3) Log received plan
    tif = getattr(sig, "tif", DEFAULT_TIF)
    log.info(
        f"[BROKER] {sig.side} {sym} band=({sig.band_low:.6f},{sig.band_high:.6f}) "
        f"{'SL=' + str(sig.stop_loss) if getattr(sig, 'stop_loss', None) else ''} "
        f"lev={getattr(sig, 'leverage', None)} TIF={tif}"
    )

    # 4) Price discovery
    mark = _get_mark_price(coin)
    if mark is not None:
        log.info(f"[PRICE] {coin} mark={mark}")
    else:
        log.info(f"[PRICE] mark fetch failed for {coin}; sizing from entry px")

    # 5) Build order plan
    side_u = sig.side.strip().upper()
    is_buy = side_u in ("BUY", "LONG")

    entry_px = _pick_entry_price(is_buy, float(sig.band_low), float(sig.band_high))
    px_str = f"{entry_px:.8f}".rstrip("0").rstrip(".")
    # size by USD (simple)
    use_px = mark if mark is not None else entry_px
    raw_sz = max(1e-12, USD_PER_ORDER / max(1e-12, use_px))
    raw_sz = _round_sz_to_decimals(coin, raw_sz)

    log.info(
        f"[PLAN] side={'BUY' if is_buy else 'SELL'} coin={coin} a={a} px={px_str} "
        f"sz={raw_sz:g} tif={tif} reduceOnly=False"
    )

    # 6) Place order
    resp = _place_order_real(
        coin=coin,
        asset_idx=a,
        side="BUY" if is_buy else "SELL",
        px_str=px_str,
        sz_str=f"{raw_sz:g}",
        tif=tif,
        reduce_only=False,
        cloid=None,
    )
    return resp
