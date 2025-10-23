import os
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

# ---------- Hyperliquid SDK (support multiple layouts) ----------
try:
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
except Exception:  # older SDKs
    from hyperliquid import Exchange, Info  # type: ignore

# Wallet moved between modules across versions
_Wallet = None
for _cand in ("hyperliquid.wallet", "hyperliquid.utils.wallet"):
    try:
        _mod = __import__(_cand, fromlist=["Wallet"])
        _Wallet = getattr(_mod, "Wallet")
        break
    except Exception:
        pass

# Legacy path: agent with eth-account (only used if Wallet is missing)
try:
    from eth_account import Account  # noqa: F401
    from eth_account.messages import SignableMessage  # noqa: F401
    _HAS_ETH = True
except Exception:
    _HAS_ETH = False

# ---------- Config ----------
_BASE_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}

log = logging.getLogger("broker.hyperliquid")
log.setLevel(logging.INFO)


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    if isinstance(v, str):
        v = v.strip()
    return v


def _get_allowed_symbols() -> set[str] | None:
    csv = _env("HYPER_ONLY_EXECUTE_SYMBOLS")
    if not csv:
        return None
    return {s.strip().upper() for s in csv.split(",") if s.strip()}


def _round8(x: float) -> float:
    """Round DOWN to 8 dp to avoid float_to_wire rounding guards."""
    return float(Decimal(str(x)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))


# ---------- Legacy agent for older SDKs ----------
class _EthAccountAgent:
    """
    Minimal agent compatible with older Hyperliquid SDKs:
      - must expose .address
      - must implement sign_message(SignableMessage) -> bytes
    """
    def __init__(self, priv_hex: str):
        from eth_account import Account as _Acct
        self._acct = _Acct.from_key(priv_hex)

    @property
    def address(self) -> str:
        return self._acct.address

    # NOTE: SDK will pass an eth_account.messages.SignableMessage
    def sign_message(self, signable):
        from eth_account import Account as _Acct
        sig = _Acct.sign_message(signable, private_key=self._acct.key)
        return sig.signature  # bytes


def _mk_clients():
    """Create Exchange and Info using whatever style this SDK supports."""
    priv = _env("HYPER_PRIVATE_KEY")
    if not priv:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    network = (_env("HYPER_NETWORK") or "mainnet").lower()
    base_url = _BASE_URLS.get(network, _BASE_URLS["mainnet"])

    ex = None
    last_err = None

    # Preferred path: Wallet
    if _Wallet is not None:
        wallet = _Wallet(priv)
        for ctor in (
            lambda: Exchange(wallet=wallet, base_url=base_url),
            lambda: Exchange(wallet),            # positional wallet
            lambda: Exchange(wallet=wallet),     # SDK with default base_url
        ):
            try:
                ex = ctor()
                break
            except Exception as e:
                last_err = e

    # Fallback path: legacy agent style
    if ex is None and _HAS_ETH:
        agent = _EthAccountAgent(priv)
        for ctor in (
            lambda: Exchange(agent=agent, base_url=base_url),
            lambda: Exchange(agent=agent),
            lambda: Exchange(agent),             # positional agent
        ):
            try:
                ex = ctor()
                break
            except Exception as e:
                last_err = e

    if ex is None:
        if _Wallet is None and not _HAS_ETH:
            raise RuntimeError(
                "This SDK lacks Wallet and eth-account isn't available; "
                "install either a newer hyperliquid SDK (with Wallet) or add eth-account."
            )
        raise RuntimeError(f"Could not construct Exchange with any style: {last_err}")

    # Info also changed across versions; try a couple styles
    info = None
    last_err = None
    for ctor in (lambda: Info(base_url=base_url), lambda: Info()):
        try:
            info = ctor()
            break
        except Exception as e:
            last_err = e
    if info is None:
        raise RuntimeError(f"Could not construct Info with any style: {last_err}")

    log.info("[BROKER] hyperliquid.py loaded")
    return ex, info, base_url


@dataclass
class _Plan:
    side: str          # BUY or SELL
    coin: str          # e.g., BTC
    px: float          # limit price (8dp)
    sz: float          # size in coin (8dp)
    tif: str           # "PostOnly" or "Gtc"
    reduceOnly: bool = False


def _make_plan(side: str, symbol: str, entry_low: float, entry_high: float, lev: float | None) -> _Plan:
    coin = symbol.split("/")[0].upper()
    mid = (float(entry_low) + float(entry_high)) / 2.0
    px = _round8(mid)

    # Sizing — small default notional, leverage multiplies notional target
    notional = float(_env("HYPER_NOTIONAL_USD", "50") or "50")
    if lev and float(lev) > 0:
        notional *= float(lev)
    sz = _round8(notional / px)

    tif_env = (_env("HYPER_DEFAULT_TIF") or "PostOnly").strip()
    tif = "PostOnly" if tif_env.lower() == "postonly" else "Gtc"

    return _Plan(side=side, coin=coin, px=px, sz=sz, tif=tif)


def _order_type_from_tif(tif: str) -> dict:
    # Preferred modern shape: limit tif=Gtc/Ioc/Alo
    t = tif.strip().lower()
    if t == "postonly":
        # Alo == Add Liquidity Only (post-only)
        return {"limit": {"tif": "Alo"}}
    if t in ("gtc", "ioc", "alo"):
        return {"limit": {"tif": tif if tif in ("Gtc", "Ioc", "Alo") else tif.capitalize()}}
    # default
    return {"limit": {"tif": "Gtc"}}


def _build_order(plan: _Plan) -> dict:
    return {
        "coin": plan.coin,
        "is_buy": True if plan.side == "BUY" else False,
        "sz": _round8(plan.sz),
        "limit_px": _round8(plan.px),
        "order_type": _order_type_from_tif(plan.tif),
        "reduce_only": bool(plan.reduceOnly),
    }


def _try_bulk_with_rounding(ex: "Exchange", order: dict) -> dict:
    """Call bulk_orders; if type/rounding issues arise, fix and retry."""
    order["sz"] = float(order["sz"])
    order["limit_px"] = float(order["limit_px"])

    def _bulk(o):
        return ex.bulk_orders([o])

    try:
        return _bulk(order)
    except Exception as e:
        last_err = e
        msg = str(e)

        # If this SDK/server doesn't accept {"limit":{"tif":"Alo"}}, try legacy {"postOnly": {}}
        if "Invalid order type" in msg:
            ot = order.get("order_type", {})
            if ot == {"limit": {"tif": "Alo"}}:
                # legacy fallback
                legacy = dict(order)
                legacy["order_type"] = {"postOnly": {}}
                try:
                    return _bulk(legacy)
                except Exception as e2:
                    last_err = e2

    # If we reached here, try size nudges to dodge float_to_wire guards
    step = 1e-8
    for _ in range(6):
        new_sz = max(0.0, float(order["sz"]) - step)
        if new_sz <= 0.0:
            break
        order["sz"] = _round8(new_sz)
        try:
            return ex.bulk_orders([order])
        except Exception as e:
            last_err = e

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")



def submit_signal(sig) -> None:
    """Entry point used by execution.py."""
    if getattr(sig, "entry_low", None) is None or getattr(sig, "entry_high", None) is None:
        raise ValueError("Signal missing entry_band=(low, high).")

    side_raw = str(getattr(sig, "side", "")).upper()
    if side_raw.startswith(("LONG", "BUY")):
        side = "BUY"
    elif side_raw.startswith(("SHORT", "SELL")):
        side = "SELL"
    else:
        raise ValueError(f"Unsupported side: {getattr(sig, 'side')}")

    symbol = str(getattr(sig, "symbol"))
    allowed = _get_allowed_symbols()
    if allowed is not None and symbol.upper() not in allowed:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    entry_low = float(getattr(sig, "entry_low"))
    entry_high = float(getattr(sig, "entry_high"))
    sl = getattr(sig, "stop_loss", None)
    lev = getattr(sig, "lev", None)

    ex, _info, _base = _mk_clients()

    plan = _make_plan(side, symbol, entry_low, entry_high, lev)
    log.info(
        "[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=%s",
        side, symbol, entry_low, entry_high, str(sl), str(lev), plan.tif
    )
    log.info(
        "[BROKER] PLAN side=%s coin=%s px=%0.8f sz=%0.8f tif=%s reduceOnly=%s",
        plan.side, plan.coin, plan.px, plan.sz, plan.tif, plan.reduceOnly
    )

    order = _build_order(plan)
    resp = _try_bulk_with_rounding(ex, order)
    log.info("[BROKER] bulk_orders OK: %s", resp)
