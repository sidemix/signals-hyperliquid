# bootcheck.py
import os, sys, logging, time

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("bootcheck")

# WATCH_CHANNEL_IDS or TARGET_CHANNEL_ID satisfies the Discord target requirement
REQUIRED_ENVS = {
    "DISCORD_BOT_TOKEN":       False,  # required
    "WATCH_CHANNEL_IDS":       True,   # optional
    "TARGET_CHANNEL_ID":       True,   # optional fallback
    # Hyperliquid
    "HYPER_PRIVATE_KEY":       False,  # required (0x…)
    "HYPER_ACCOUNT_ADDRESS":   False,  # required (0x… public address)
    "HYPER_ONLY_EXECUTE_SYMBOLS": True,
    "HYPER_TIF":               True,   # Alo/Ioc/Gtc (PostOnly ~= Alo)
    "HYPER_NOTIONAL_USD":      True,
    "HYPER_API_URL":           True,   # optional (defaults to MAINNET)
}

def _mask(v: str, keep=6) -> str:
    v = v or ""
    if len(v) <= keep * 2:
        return "*" * len(v)
    return v[:keep] + "…" + v[-keep:]

def run_startup_checks():
    ok = True
    log.info("[BOOT] Python %s", sys.version.split()[0])

    # Env checks
    for key, optional in REQUIRED_ENVS.items():
        val = os.getenv(key, "")
        if val:
            if key in ("HYPER_PRIVATE_KEY", "HYPER_ACCOUNT_ADDRESS"):
                log.info("[BOOT] %s=%s", key, _mask(val))
            else:
                log.info("[BOOT] %s=%r", key, val)
        elif not optional:
            log.error("[BOOT] Missing required env: %s", key)
            ok = False

    # SDK import smoke test (0.20.x layout)
    try:
        import hyperliquid.exchange, hyperliquid.info, hyperliquid.utils.constants  # noqa: F401
        import eth_account  # signer comes from eth_account
        log.info("[BOOT] hyperliquid SDK import OK")
    except Exception as e:
        log.exception("[BOOT] hyperliquid SDK import FAILED: %s", e)
        ok = False

    if not ok:
        log.error("[BOOT] Startup checks failed—fix the errors above.")
        time.sleep(120)  # keep container alive so you can read logs
        sys.exit(1)
