import os
import logging
from typing import Dict, Any, Optional, Tuple

# --- add/confirm these imports at the top ---
from typing import Optional, Tuple, Any
import os, logging as log

# NEW imports:
from eth_account import Account
from eth_account.messages import encode_defunct, SignableMessage

# Try to use SDK’s wallet if present
try:
    from hyperliquid.utils.wallet import Wallet  # type: ignore
    _HAS_SDK_WALLET = True
except Exception:
    _HAS_SDK_WALLET = False

# ---------- Logging ----------
log = logging.getLogger("broker.hyperliquid")
if not log.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# ---------- SDK imports (robust across versions) ----------
# Exchange / Info live on submodules in recent SDKs.
try:
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.info import Info          # type: ignore
except Exception as e:  # pragma: no cover
    raise ImportError(
        "Could not import hyperliquid SDK (exchange/info). "
        "Make sure 'hyperliquid-python-sdk' is installed."
    ) from e

# Try wallet/signer classes across SDK variants; if none exist we'll build our own with eth-account
_SDK_WALLET_CLS = None
for cand in (
    "hyperliquid.utils.wallet",
    "hyperliquid.wallet",
    "hyperliquid.utils.signing",  # some older builds exposed a signer here
):
    try:
        mod = __import__(cand, fromlist=["Wallet"])
        if hasattr(mod, "Wallet"):
            _SDK_WALLET_CLS = getattr(mod, "Wallet")
            break
    except Exception:
        pass

# ---------- Environment ----------
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "50"))
DEFAULT_TIF   = os.getenv("HYPER_TIF", "PostOnly")  # "PostOnly" or "Gtc"

ALLOWED = [s.strip().upper() for s in os.getenv(
    "HYPER_ONLY_EXECUTE_SYMBOLS",
    "AVAX/USD,BIO/USD,BNB/USD,BTC/USD,CRV/USD,ETH/USD,ETHFI/USD,LINK/USD,MNT/USD,"
    "PAXG/USD,SNX/USD,SOL/USD,STBL/USD,TAO/USD,ZORA/USD"
).split(",") if s.strip()]

BASE_URL = os.getenv("HYPER_BASE_URL")  # optional, SDK typically defaults correctly
WS_URL   = os.getenv("HYPER_WS_URL")    # optional

# ---------- Helpers ----------
def _symbol_to_coin(symbol: str) -> str:
    # "BTC/USD" -> "BTC"
    if "/" in symbol:
        return symbol.split("/")[0].strip().upper()
    return symbol.strip().upper()

def _choose_px(entry_low: float, entry_high: float, side: str) -> float:
    # Use inner edge of the band consistent with side
    return float(entry_low if str(side).upper() == "LONG" else entry_high)

def _quantize_attempts() -> Tuple[list[int], list[int]]:
    # Decimals for (price_decimals, size_decimals) to try in order
    return [8, 7, 6, 5, 4, 3, 2], [8, 7, 6, 5, 4, 3, 2]

# --- replace your current _mk_agent_from_privkey + agent class with this ---

def _mk_agent_from_privkey(priv: str) -> Any:
    """
    Prefer the SDK's Wallet (if available). Otherwise fall back to a minimal
    eth-account based agent that matches the interface the SDK expects.
    """
    if _HAS_SDK_WALLET:
        return Wallet(priv)  # SDK will call .sign_message()/etc. on this
    return _EthAccountAgent(priv)


class _EthAccountAgent:
    """
    Minimal signer compatible with hyperliquid’s Exchange expectations.
    Handles both str payloads and already-constructed SignableMessage.
    """
    def __init__(self, priv: str):
        self._acct = Account.from_key(priv)

    # The SDK calls this; make it robust to multiple input types.
    def sign_message(self, msg: Any) -> dict:
        """
        Accepts:
          - str: will be signed as EIP-191 defunct message (same as SDK’s simple path)
          - bytes: same, by decoding as utf-8
          - SignableMessage: passed directly to Account.sign_message
        Returns dict with 'signature' hex (what SDK expects).
        """
        if isinstance(msg, SignableMessage):
            signed = self._acct.sign_message(msg)
        elif isinstance(msg, (bytes, bytearray)):
            signed = self._acct.sign_message(encode_defunct(text=msg.decode("utf-8")))
        elif isinstance(msg, str):
            signed = self._acct.sign_message(encode_defunct(text=msg))
        else:
            # Some SDK builds pass {"message": "..."} or similar — handle gently.
            try:
                # Try to stringify sensibly
                signed = self._acct.sign_message(encode_defunct(text=str(msg)))
            except Exception as e:
                raise TypeError(f"Unsupported message type for signing: {type(msg)}") from e

        # SDK expects a dict-like with 'signature' (hex string)
        return {"signature": signed.signature.hex()}

    # Some SDKs also probe sign_typed_data; provide a safe stub.
    def sign_typed_data(self, typed_data: dict) -> dict:
        # If you later need true EIP-712 support, wire it here.
        raise NotImplementedError("EIP-712 signing not implemented in fallback agent")

    # ---- Minimal agent using eth-account ----
    from eth_account import Account
    from eth_account.messages import encode_defunct

    class _EthAccountAgent:
        def __init__(self, pk: str):
            # Account.from_key accepts hex string with or without 0x
            self._acct = Account.from_key(pk)

        # hyperliquid SDK calls agent.sign_message(message_str)
        def sign_message(self, message: str) -> str:
            msg = encode_defunct(text=message)
            sig = self._acct.sign_message(msg)
            # SDK generally accepts hex signature
            return sig.signature.hex()

        # Some SDKs also read address property
        @property
        def address(self) -> str:
            return self._acct.address

    return _EthAccountAgent(priv)

def _mk_clients() -> Tuple[Exchange, Info]:
    # Wallet private key auth (required)
    priv = os.getenv("HYPER_PRIVATE_KEY", "").strip()
    if not priv:
        raise RuntimeError("No Hyperliquid credentials found. Set HYPER_PRIVATE_KEY (wallet private key).")

    agent = _mk_agent_from_privkey(priv)

    ex: Optional[Exchange] = None
    init_errors = []

    # Try common constructor styles

    # (a) keyword args
    try:
        kwargs = {"agent": agent}
        if BASE_URL:
            kwargs["base_url"] = BASE_URL
        if WS_URL:
            kwargs["websocket_url"] = WS_URL
        ex = Exchange(**kwargs)  # type: ignore[arg-type]
    except Exception as e:
        init_errors.append(f"agent-kwargs: {e}")

    # (b) positional agent
    if ex is None:
        try:
            ex = Exchange(agent)  # type: ignore[misc]
        except Exception as e:
            init_errors.append(f"agent-positional: {e}")

    # (c) very old builds: positional privkey
    if ex is None:
        try:
            ex = Exchange(priv)  # type: ignore[misc]
        except Exception as e:
            init_errors.append(f"privkey-positional: {e}")

    if ex is None:
        raise RuntimeError("Failed to initialize Exchange with provided private key. "
                           f"Attempts: {', '.join(init_errors)}")

    # --- IMPORTANT ---
    # Some SDK builds don't keep the agent we pass. Make sure ex.agent is our signer.
    try:  # <<< NEW >>>
        setattr(ex, "agent", agent)      # force the correct agent onto the Exchange  <<< NEW >>>
    except Exception:
        pass                              # <<< NEW >>>

    # Optional: log what we ended up with (helps confirm)
    try:  # <<< NEW >>>
        atype = type(getattr(ex, "agent", None)).__name__
        log.info("[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY=%s…%s (agent=%s)",
                 priv[:6], priv[-4:], atype)
    except Exception:
        log.info("[BROKER] Exchange init via wallet with HYPER_PRIVATE_KEY=%s…%s",
                 priv[:6], priv[-4:])

    # Info client
    try:
        info = Info(base_url=BASE_URL) if BASE_URL else Info()
    except TypeError:
        info = Info()

    return ex, info


def _build_order_dict(coin: str, is_buy: bool, sz: float, limit_px: float, tif: str) -> Dict[str, Any]:
    # Dictionary format expected by Exchange.bulk_orders([...])
    tif_norm = "PostOnly" if str(tif) == "PostOnly" else "Gtc"
    return {
        "coin": coin,                    # name (not asset id)
        "is_buy": bool(is_buy),
        "sz": float(sz),                 # must match coin step
        "limit_px": float(limit_px),     # must match price tick
        "order_type": {"limit": {"tif": tif_norm}},  # e.g., "PostOnly" or "Gtc"
        "reduce_only": False,
        "cloid": None,
    }

def _try_bulk_with_rounding(ex: Exchange, order: Dict[str, Any]) -> Any:
    """
    Retry bulk_orders by progressively reducing precision on sz/limit_px
    until SDK accepts (avoids `float_to_wire causes rounding`).
    """
    px_attempts, sz_attempts = _quantize_attempts()
    last_err: Optional[Exception] = None

    for pd in px_attempts:
        for sd in sz_attempts:
            try:
                order_try = dict(order)
                order_try["limit_px"] = float(round(order["limit_px"], pd))
                order_try["sz"] = float(round(order["sz"], sd))
                if order_try["sz"] <= 0:
                    continue
                return ex.bulk_orders([order_try])
            except Exception as e:
                last_err = e

    # One last coarse attempt at 1–3 decimals for price & 3–5 for size
    for pd in (3, 2, 1):
        for sd in (5, 4, 3):
            try:
                order_try = dict(order)
                order_try["limit_px"] = float(round(order["limit_px"], pd))
                order_try["sz"] = float(round(order["sz"], sd))
                if order_try["sz"] <= 0:
                    continue
                return ex.bulk_orders([order_try])
            except Exception as e:
                last_err = e

    raise RuntimeError(f"SDK bulk_orders failed after rounding attempts: {last_err}")

# ---------- Public entry ----------
def submit_signal(sig) -> None:
    """
    Expected fields on `sig` (ExecSignal):
      - side: "LONG" or "SHORT"
      - symbol: like "BTC/USD"
      - entry_low, entry_high: floats
      - stop_loss: Optional[float]
      - leverage: Optional[float]
      - tpn: Optional[int]
      - timeframe: Optional[str]
      - tif: Optional[str]
    """
    side = str(getattr(sig, "side")).upper()
    symbol = str(getattr(sig, "symbol")).upper()

    tif = getattr(sig, "tif", None) or DEFAULT_TIF
    if tif not in ("PostOnly", "Gtc"):
        tif = DEFAULT_TIF

    if ALLOWED and symbol not in ALLOWED:
        log.info("[BROKER] Skipping symbol not in HYPER_ONLY_EXECUTE_SYMBOLS: %s", symbol)
        return

    entry_low = float(getattr(sig, "entry_low"))
    entry_high = float(getattr(sig, "entry_high"))
    is_buy = True if side == "LONG" else False
    coin = _symbol_to_coin(symbol)
    px = _choose_px(entry_low, entry_high, side)

    ex, info = _mk_clients()

    # Compute size ~ USD_PER_TRADE / price
    if px <= 0:
        raise RuntimeError("Invalid entry price computed for order size.")
    sz = USD_PER_TRADE / px

    order = _build_order_dict(coin=coin, is_buy=is_buy, sz=sz, limit_px=px, tif=tif)

    log.info(
        "[BROKER] %s %s band=(%f,%f) SL=%s lev=%s TIF=%s",
        side, symbol, entry_low, entry_high,
        str(getattr(sig, "stop_loss", None)),
        str(getattr(sig, "leverage", None)),
        tif,
    )

    # Plan log (mid-band px for visibility only)
    mid_px = (entry_low + entry_high) / 2.0
    sz_preview = USD_PER_TRADE / max(px, 1e-9)
    log.info("[PLAN] side=%s coin=%s px=%0.8f sz=%s tif=%s reduceOnly=False",
             "BUY" if is_buy else "SELL", coin, mid_px, f"{sz_preview:.8f}", tif)

    try:
        resp = _try_bulk_with_rounding(ex, order)
        log.info("[BROKER] bulk_orders response: %s", str(resp))
    except Exception as e:
        raise RuntimeError(f"SDK bulk_orders failed: {e}") from e
