# broker/hyperliquid.py
# Drop-in broker for Hyperliquid perps using the official Python SDK.
# - Places real orders (no stub)
# - Handles asset discovery, precision, leverage, TP/SL grouping
# - Respects DRY_RUN and environment sizing knobs
#
# Tyler: this file assumes your parser builds a "Signal" dict like the one logged in execution.py:
#   {
#     "side": "LONG" | "SHORT",
#     "symbol": "ETH/USD",
#     "band": (entry_min, entry_max),
#     "sl": float,
#     "tpn": int,           # number of take profits
#     "lev": float,
#     "tf": "5m"
#   }
# And execution.py calls: submit_signal(signal)

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List

# --- SDK imports
try:
    from hyperliquid.exchange import Exchange, OrderRequest, Tif, Trigger, TriggerType
    from hyperliquid.info import Info, Env as HlEnv
except Exception as e:
    raise ImportError(
        "hyperliquid-python-sdk is required. Add 'hyperliquid-python-sdk' to requirements.txt. "
        f"Import error: {e}"
    )

# --- Logging
import logging
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# =========================
# Environment
# =========================

ENV_HYPER_BASE       = os.getenv("HYPERLIQUID_BASE", "https://api.hyperliquid.xyz").rstrip("/")
ENV_NETWORK          = os.getenv("HYPER_NETWORK", "mainnet").lower()  # 'mainnet' | 'testnet'
ENV_DRY_RUN          = os.getenv("DRY_RUN", "false").lower() == "true"

ENV_FIXED_QTY        = float(os.getenv("HYPER_FIXED_QTY", "0"))       # if >0, use coin units
ENV_TRADE_SIZE_USD   = float(os.getenv("TRADE_SIZE_USD", "0"))        # else size by notional
ENV_TP_WEIGHTS       = os.getenv("TP_WEIGHTS", "0.10,0.15,0.15,0.20,0.20,0.20")
ENV_ONLY_EXECUTE     = os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "")    # "ETH/USD,BTC/USD,..."
ENV_ACCOUNT_MODE     = os.getenv("ACCOUNT_MODE", "perp").lower()      # perp | spot
ENV_EXECUTION_MODE   = os.getenv("XECUTION_MODE", "OTO").upper()      # OTO, LIMIT_ONLY, IOC_ONLY
ENV_ENTRY_TIMEOUT_MIN= int(os.getenv("ENTRY_TIMEOUT_MIN", "120"))

# Keys
ENV_EVM_PRIVKEY      = os.getenv("HYPER_EVM_PRIVKEY", "")             # 0x...
ENV_CHAIN_ID         = int(os.getenv("HYPER_EVM_CHAIN_ID", "999"))    # 999 mainnet, 998 testnet
ENV_EVM_RPC          = os.getenv("HYPER_EVM_RPC", "https://rpc.hyperliquid.xyz/evm")

if not ENV_EVM_PRIVKEY and not ENV_DRY_RUN:
    raise RuntimeError("HYPER_EVM_PRIVKEY is required for live trading. Set DRY_RUN=true to simulate.")


# =========================
# Helpers / dataclasses
# =========================

@dataclass
class AssetMeta:
    index: int
    sz_decimals: int


class HyperliquidBroker:
    """
    Thin wrapper around Hyperliquid SDK: asset discovery, mark fetch, precision, placement.
    """

    def __init__(self):
        self._env = HlEnv.Mainnet if ENV_NETWORK == "mainnet" else HlEnv.Testnet
        self._base = ENV_HYPER_BASE
        self._info = Info(self._env, base_url=self._base)
        self._exch = Exchange(
            private_key=ENV_EVM_PRIVKEY if ENV_EVM_PRIVKEY else None,
            env=self._env,
            base_url=self._base,
            evm_rpc_url=ENV_EVM_RPC,
            chain_id=ENV_CHAIN_ID,
        )
        self._asset_meta_cache: Dict[str, AssetMeta] = {}
        self._tp_weights: List[float] = self._parse_tp_weights(ENV_TP_WEIGHTS)
        self._allowed: Optional[set] = (
            set([s.strip().upper() for s in ENV_ONLY_EXECUTE.split(",") if s.strip()])
            if ENV_ONLY_EXECUTE else None
        )
        log.info(f"[HL] env={self._env.name} base={self._base} dry_run={ENV_DRY_RUN} exec_mode={ENV_EXECUTION_MODE}")
        self._warm_meta()

    # ---------- public ----------

    def submit_signal(self, sig: Dict):
        """
        Entry point called by execution.py.
        """
        try:
            symbol = sig.get("symbol", "").upper()
            if self._allowed is not None and symbol not in self._allowed:
                log.info(f"[BROKE]()
