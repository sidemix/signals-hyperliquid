# broker/hyperliquid.py
from __future__ import annotations

import os
import math
import logging
from typing import Any, Dict, Optional, Tuple

__VERSION__ = "hl-broker-1.2.1"

log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(levelname)s:%(name)s:%(message)s")
    h.setFormatter(fmt)
    log.addHandler(h)
log.setLevel(logging.INFO)
log.info(f"[BROKER] hyperliquid.py loaded, version={__VERSION__}")

# -----------------------
# Env helpers
# -----------------------

def _getenv_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _getenv_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "").strip() or default)
    except Exception:
        return default

# Core env
HYPER_DRY_RUN = _getenv_bool("HYPER_DRY_RUN", False) or _getenv_bool("hyper_dry_run", False)
HL_NETWORK = (os.getenv("HL_NETWORK") or os.getenv("HYPER_NETWORK") or "mainnet").strip().lower()
USER_ADDRESS = (os.getenv("HL_ADDRESS") or os.getenv("HYPER_USER_ADDRESS") or "").strip() or None
VAULT_ADDRESS = (os.getenv("HL_VAULT_ADDRESS") or os.getenv("HYPER_VAULT_ADDRESS") or "").strip() or None

AGENT_WALLET_PK = (
    os.getenv("HL_AGENT_WALLET_PK")
    or os.getenv("HL_API_WALLET_PK")
    or os.getenv("HYPER_AGENT_PK")
    or os.getenv("HYPER_API_WALLET_PK")
    or os.getenv("HL_PRIVATE_KEY")
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
_CHAIN: Optional[Any] = None

def _resolve_chain() -> Any:
    """
    Force the SDK's Chain enum; do NOT fall back to strings (that breaks the SDK’s ws url derivation).
    """
    global _CHAIN
    if _CHAIN is not None:
        return _CHAIN
    try:
        from hyperliquid.exchange import Chain
    except Exception as e:
        raise RuntimeError(
            "Failed to import hyperliquid.exchange.Chain from the SDK. "
            "Ensure hyperliquid-python-sdk is installed."
        ) from e
    _CHAIN = Chain.TESTNET if HL_NETWORK.startswith("test") else Chain.MAINNET
    return _CHAIN

def _get_info():
    """Cache an Info client constructed with Chain enum (prevents invalid ws url)."""
    global _INFO
    if _INFO is not None:
        return _INFO
    chain = _resolve_chain()
    try:
        from hyperliquid.info import Info
        _INFO = Info(chain)
        return _INFO
    except TypeError:
        # Older SDKs that take a base-url only
        try:
            from hyperliquid.info import Info
            base = "https://api.hyperliquid-testnet.xyz" if HL_NETWORK.startswith("test") else "https://api.hyperliquid.xyz"
            _INFO = Info(base)
            return _INFO
        except Exception as e:
            raise RuntimeError(f"Unable to construct Info client: {e}") from e

def _get_exchange():
    """
    Cache an Exchange client using the Chain enum.
    If your SDK supports Chain, we pass it. We do not pass empty/invalid urls.
    """
    global _EXC
    if _EXC is not None:
        return _EXC
    if not AGENT_WALLET_PK:
        raise RuntimeError("No agent/API wallet private key set. Set HL_AGENT_WALLET_PK (hex 0x...).")
    chain = _resolve_chain()
    from hyperliquid.exchange import Exchange
    last_err: Optional[Exception] = None
    # Try clean signatures first
    for kwargs in (
        {"private_key": AGENT_WALLET_PK, "chain": chain},
        {"key": AGENT_WALLET_PK, "chain": chain},  # some older builds used 'key'
        {"private_key": AGENT_WALLET_PK, "account_address": USER_ADDRESS, "chain": chain},
    ):
        try:
            _EXC = Exchange(**{k: v for k, v in kwargs.items() if v is not None})
            return _EXC
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Unable to construct Exchange client: {last_err}")

# -------------------------
# Helpers
# -------------------------

def _normalize_symbol(raw: str) -> Tuple[str, str]:
    sym = raw.strip().upper().replace("USDT", "USD").replace("PERP", "").replace("-PERP", "")
    coin = sym.split("/")[0].strip()
    return coin, sym

def _get_asset_index(coin: str) -> int:
    info = _get_info()
    a = info.name_to_asset(coin)
    if a is None:
        raise RuntimeError(f"Unknown coin {coin}")
    return int(a)

def _get_mark_price(coin: str) -> Optional[float]:
    info = _get_info()
    try:
        if hasattr(info, "active_asset_ctx"):
            ctx = info.active_asset_ctx(coin)
            if ctx and "ctx" in ctx and ctx["ctx"] and "markPx" in ctx["ctx"]:
                return float(ctx["ctx"]["markPx"])
    except Exception as e:
        log.warning(f"active_asset_ctx failed for {coin}: {e}")
    try:
        if hasattr(info, "all_mids"):
            mids = info.all_mids()
            mid_s = mids.get(coin) if isinstance(mids, dict) else None
            if mid_s is not None:
                return float(mid_s)
    except Exception as e:
        log.warning(f"all_mids failed for {coin}: {e}")
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

def _round_sz_to_decimals(coin: str, sz: float) -> float:
    try:
        info = _get_info()
        meta = info.meta() if callable(getattr(info, "meta", None)) else None
        if meta and "universe" in meta:
            for a in meta["universe"]:
                if a.get("name") == coin and "szDecimals" in a:
                    d = int(a["szDecimals"])
                    factor = 10 ** d
                    return math.floor(sz * factor + 1e-9) / factor
    except Exception:
        pass
    return sz

def _mk_order_type(tif: str = "Gtc"):
    tif_norm = (tif or "Gtc").strip().lower()
    tif_norm = {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}.get(tif_norm, "Gtc")
    from hyperliquid.utils.signing import OrderType, TimeInForce  # raises if missing
    tif_enum = getattr(TimeInForce, tif_norm)
    return OrderType.Limit(tif_enum)

def _pick_entry_price(is_buy: bool, band_low: float, band_high: float) -> float:
    return band_low if is_buy else band_high

# --------------------------
# Order placement (positional SDK)
# --------------------------

def _place_order_real(
    *,
    coin: str,
    is_buy: bool,
    limit_px: float,
    sz: float,
    tif: str = "Gtc",
    reduce_only: bool = False,
    cloid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calls SDK signature:
      Exchange.order(
        name: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: hyperliquid.utils.signing.OrderType,
        reduce_only: bool = False,
        cloid: Optional[hyperliquid.utils.types.Cloid] = None,
        builder: Optional[hyperliquid.utils.types.BuilderInfo] = None
      )
    """
    ex = _get_exchange()
    order_type = _mk_order_type(tif)

    if HYPER_DRY_RUN:
        log.info(f"[DRYRUN] submit LIMIT {'BUY' if is_buy else 'SELL'} {coin} px={limit_px} sz={sz} tif={tif} reduceOnly={reduce_only}")
        return {"status": "ok", "dryRun": True}

    args = (coin, bool(is_buy), float(sz), float(limit_px), order_type)
    kwargs: Dict[str, Any] = {"reduce_only": bool(reduce_only)}
    if cloid:
        kwargs["cloid"] = cloid
    resp = ex.order(*args, **kwargs)  # type: ignore[misc]
    log.info(f"[BROKER] order response: {resp}")

    if isinstance(resp, dict):
        if resp.get("status") == "ok" or resp.get("success") is True:
            return {"status": "ok", "response": resp}
        raise RuntimeError(f"Order rejected: {resp}")
    if resp:
        return {"status": "ok", "response": resp}
    raise RuntimeError(f"Unexpected falsy response: {resp!r}")

# --------------------------
# Public entry from execution.py
# --------------------------

def submit_signal(sig) -> Dict[str, Any]:
    """
    Expects from ExecSignal:
      - side: 'LONG'/'SHORT' or 'BUY'/'SELL'
      - symbol: 'ZRO/USD'
      - band_low, band_high: floats
      - stop_loss: float (optional)
      - leverage: float (optional)
      - timeframe: str (optional)
      - tif: str (optional)
    """
    coin, sym = _normalize_symbol(sig.symbol)

    if ALLOWED_SET and sym.upper() not in ALLOWED_SET and coin.upper() not in ALLOWED_SET:
        log.info(f"[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: {sym}")
        return {"status": "skipped", "reason": "symbol not allowed", "symbol": sym}

    log.info(f"[BROKER] symbol={sig.symbol} allowed={os.getenv('HYPER_ONLY_EXECUTE_SYMBOLS','*')}")


    # Resolve asset index for sanity/logging (not passed to order())
    try:
        a = _get_asset_index(coin)
    except Exception as e:
        return {"status": "error", "reason": f"asset index lookup failed: {e}"}

    tif = getattr(sig, "tif", DEFAULT_TIF)
    sl = getattr(sig, "stop_loss", None)
    lev = getattr(sig, "leverage", None)

    log.info(
        f"[BROKER] {sig.side} {sym} band=({sig.band_low:.6f},{sig.band_high:.6f}) "
        f"{'SL=' + str(sl) if sl is not None else ''} lev={lev if lev is not None else ''} TIF={tif}"
    )

    mark = _get_mark_price(coin)
    if mark is not None:
        log.info(f"[PRICE] {coin} mark={mark}")
    else:
        log.info(f"[PRICE] mark fetch failed for {coin}; will size from entry px")

    is_buy = sig.side.strip().upper() in ("BUY", "LONG")
    entry_px = _pick_entry_price(is_buy, float(sig.band_low), float(sig.band_high))
    use_px = mark if mark is not None else entry_px
    raw_sz = max(1e-12, USD_PER_ORDER / max(1e-12, use_px))
    raw_sz = _round_sz_to_decimals(coin, raw_sz)

    log.info(
        f"[PLAN] side={'BUY' if is_buy else 'SELL'} coin={coin} a={a} px={entry_px:.8f} "
        f"sz={raw_sz:g} tif={tif} reduceOnly=False"
    )

    # Place order with positional SDK
    return _place_order_real(
        coin=coin,
        is_buy=is_buy,
        limit_px=float(entry_px),
        sz=float(raw_sz),
        tif=tif,
        reduce_only=False,
        cloid=None,
    )
