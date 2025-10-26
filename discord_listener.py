import os
import sys
import uuid
import logging

from bootcheck import run_startup_checks
run_startup_checks()

from typing import Optional, Tuple
import discord

from execution import ExecSignal, execute_signal
from parser import parse_signal

# ---------- Logging ----------
root_level = os.getenv("LOG_LEVEL", "INFO").upper()
# Configure once
if not logging.getLogger().handlers:
    logging.basicConfig(level=root_level, stream=sys.stdout)
log = logging.getLogger("discord_listener")
logging.getLogger("discord").setLevel(root_level)
logging.getLogger("discord.client").setLevel(root_level)
logging.getLogger("discord.gateway").setLevel(root_level)
logging.getLogger("discord.http").setLevel(root_level)

# Unique id for this process (helps detect >1 instance)
PROCESS_ID = os.getenv("INSTANCE_ID") or str(uuid.uuid4())[:8]

# ---------- Env ----------
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is missing")

watch_ids_env = (os.getenv("WATCH_CHANNEL_IDS", "") or "").strip()
WATCH_CHANNEL_IDS = {int(x.strip()) for x in watch_ids_env.split(",") if x.strip()}
if not WATCH_CHANNEL_IDS:
    t = (os.getenv("TARGET_CHANNEL_ID", "") or "").strip()
    if t:
        WATCH_CHANNEL_IDS = {int(t)}
    else:
        raise RuntimeError("Set WATCH_CHANNEL_IDS (comma-separated) or TARGET_CHANNEL_ID")

POST_CHANNEL_ID = int(os.getenv("POST_CHANNEL_ID", str(next(iter(WATCH_CHANNEL_IDS)))))

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True   # enable in Dev Portal too
intents.guilds = True
client = discord.Client(intents=intents)

# ---------- Strong duplicate guard (per-process) ----------
_SEEN_MSG_IDS: set[int] = set()

def _seen(msg_id: int) -> bool:
    """Return True if we've already processed this message id in this process."""
    if msg_id in _SEEN_MSG_IDS:
        return True
    _SEEN_MSG_IDS.add(msg_id)
    # keep memory bounded
    if len(_SEEN_MSG_IDS) > 20000:
        _SEEN_MSG_IDS.clear()
    return False

def _coerce_entry_band(parsed) -> Tuple[Optional[float], Optional[float]]:
    low = getattr(parsed, "entry_low", None)
    high = getattr(parsed, "entry_high", None)
    try: low = float(low) if low is not None else None
    except Exception: low = None
    try: high = float(high) if high is not None else None
    except Exception: high = None
    return low, high

@client.event
async def on_ready():
    try:
        ch = await client.fetch_channel(POST_CHANNEL_ID)
        log.info(
            "[READY][proc=%s] Logged in as %s | watching=%s | posting-> #%s (%s)",
            PROCESS_ID, client.user, sorted(WATCH_CHANNEL_IDS), getattr(ch, "name", "?"), POST_CHANNEL_ID
        )
        # Silent mode: no public message in the channel
    except Exception as e:
        log.exception("[READY] Failed: %s", e)

@client.event
async def on_connect():
    log.info("[GATEWAY][proc=%s] Connected to Discord gateway.", PROCESS_ID)

@client.event
async def on_message(message: discord.Message):
    try:
        # ignore self/bots and other channels
        if message.author == client.user or getattr(message.author, "bot", False):
            return
        if message.channel.id not in WATCH_CHANNEL_IDS:
            return

        # dedupe by Discord message id BEFORE any parsing
        if _seen(message.id):
            log.info("[RX][proc=%s] Duplicate message id=%s (skipping).", PROCESS_ID, message.id)
            return

        content = message.content or ""
        log.info("[RX][proc=%s] ch=%s by=%s id=%s len=%d",
                 PROCESS_ID, message.channel.id, message.author, message.id, len(content))

        parsed = parse_signal(content)
        if not parsed:
            log.info("[RX] parse_signal returned None (skipping).")
            return

        entry_low, entry_high = _coerce_entry_band(parsed)
        if entry_low is None or entry_high is None:
            log.info("[RX] Missing entry band after coercion: low=%s high=%s", entry_low, entry_high)
            return

        # Set idempotency key so two triggers can't double-open inside a process
        setattr(parsed, "client_id", f"discord:{message.id}")

        log.info(
            "[PARSER][proc=%s] side=%s symbol=%s band=(%.6f, %.6f) sl=%s lev=%s tif=%s client_id=%s",
            PROCESS_ID,
            getattr(parsed, "side", None),
            getattr(parsed, "symbol", None),
            entry_low, entry_high,
            getattr(parsed, "stop_loss", None),
            getattr(parsed, "leverage", None),
            getattr(parsed, "tif", None),
            getattr(parsed, "client_id", None),
        )

        # Execute (silent: no message posted to the channel)
        resp = execute_signal(parsed)
        log.info("[EXEC][proc=%s] execute_signal returned: %s", PROCESS_ID, resp)

        # If you want a private DM to yourself, uncomment:
        # if str(message.author.name).lower() == "yourname":
        #     await message.author.send(f"✅ Executed: {parsed.side} {parsed.symbol}")

    except Exception as e:
        log.exception("[RX] Unhandled error in on_message: %s", e)

if __name__ == "__main__":
    try:
        log.info("[BOOT][proc=%s] Starting Discord client…", PROCESS_ID)
        client.run(BOT_TOKEN, log_handler=None)
    except Exception as e:
        log.exception("[FATAL] client.run failed: %s", e)
        raise
