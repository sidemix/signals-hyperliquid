# bootcheck.py
import os, sys, logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bootcheck")

REQUIRED_ENVS = {
    "DISCORD_BOT_TOKEN":    False,   # required
    "TARGET_CHANNEL_ID":    False,   # required (the text channel your bot posts into)
    "HYPER_PRIVATE_KEY":    False,   # required (0x…)
    "HYPER_ONLY_EXECUTE_SYMBOLS": True,  # optional but useful (e.g., BTC,ETH or BTC/USD,ETH/USD)
    "HYPER_TIF":            True,    # optional (Alo/Ioc/Gtc)
    "HYPER_NOTIONAL_USD":   True,    # optional (default 50)
}

def _mask(v: str, keep=4):
    v = v or ""
    if len(v) <= keep*2:
        return "*" * len(v)
    return v[:keep] + "…" + v[-keep:]

def run_startup_checks():
    log.info("[BOOT] Python %s", sys.version.split()[0])
    # Check envs
    ok = True
    for key, optional in REQUIRED_ENVS.items():
        val = os.getenv(key, "")
        if val:
            if key in ("HYPER_PRIVATE_KEY",):
                log.info("[BOOT] %s = %s", key, _mask(val, 6))
            else:
                log.info("[BOOT] %s = %r", key, val)
        elif not optional:
            log.error("[BOOT] MISSING required env: %s", key)
            ok = False

    # Try SDK import quickly
    try:
        import hyperliquid, hyperliquid.info, hyperliquid.exchange, hyperliquid.wallet  # noqa
        log.info("[BOOT] hyperliquid SDK import OK")
    except Exception as e:
        log.exception("[BOOT] hyperliquid SDK import FAILED: %s", e)
        ok = False

    if not ok:
        log.error("[BOOT] Critical boot checks failed. The process would exit—fix envs above.")
        # Sleep a bit so logs are visible instead of instant exit
        import time; time.sleep(120)
        sys.exit(1)
