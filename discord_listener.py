# discord_listener.py
import logging
import os
import re
import discord

from execution import ExecSignal, execute_signal

log = logging.getLogger("discord_listener")
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"), format="%(levelname)s:%(name)s:%(message)s")

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# --- very small parser tuned to your VIP format --------------------------------
SIDE_PAT = re.compile(r"\b(long|short)\b", re.I)
SYMBOL_PAT = re.compile(r"Name:\s*([A-Z0-9]+/[A-Z0-9]+)", re.I)
BAND_PAT = re.compile(r"Entry Price.*?:\s*([0-9.]+)\s*[-–]\s*([0-9.]+)", re.I)
SL_PAT = re.compile(r"StopLoss:\s*([0-9.]+)", re.I)
LEV_PAT = re.compile(r"Leverage.*?\b(\d+(?:\.\d+)?)x\b", re.I)
TF_PAT = re.compile(r"TF:\s*([0-9]+[smhd])", re.I)

def parse_message(text: str):
    side = SIDE_PAT.search(text)
    symbol = SYMBOL_PAT.search(text)
    band = BAND_PAT.search(text)
    sl = SL_PAT.search(text)

    if not (side and symbol and band and sl):
        return None

    lev = LEV_PAT.search(text)
    tf = TF_PAT.search(text)

    return {
        "side": side.group(1).upper(),
        "symbol": symbol.group(1).upper(),
        "entry_low": float(band.group(1)),
        "entry_high": float(band.group(2)),
        "stop_loss": float(sl.group(1)),
        "leverage": float(lev.group(1)) if lev else 0.0,
        "tf": tf.group(1) if tf else "5m",
        "tp_count": 0,
    }

@client.event
async def on_ready():
    log.info("[READY] Logged in as %s | target CHANNEL_ID=%s", client.user, TARGET_CHANNEL_ID)
    try:
        chan = await client.fetch_channel(TARGET_CHANNEL_ID)
        await chan.send("👋 Ready. I’ll execute on allowed symbols.")
        log.info("[READY] Sent hello message successfully.")
        log.info("[READY] Resolved channel: %s type=%s", getattr(chan, 'name', 'unknown'), getattr(chan, 'type', 'text'))
    except Exception as e:
        log.warning("Could not send hello: %s", e)

@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages
    if message.author == client.user:
        log.info("[DROP] our own message")
        return

    # Only process the configured channel
    if message.channel.id != TARGET_CHANNEL_ID:
        return

    text = message.content or ""
    log.info("[RX] msg_id=%s author='%s' chan_id=%s chan_name=%s len=%s",
             message.id, getattr(message.author, "name", "unknown"), message.channel.id,
             getattr(message.channel, "name", ""), len(text))

    parsed = parse_message(text)
    if not parsed:
        log.info("[SKIP] Could not parse signal from message.")
        return

    log.info("[PASS] parsed: %s %s band=(%s, %s) SL=%s TPn=%s lev=%s TF=%s",
             parsed["side"], parsed["symbol"], parsed["entry_low"], parsed["entry_high"],
             parsed["stop_loss"], parsed["tp_count"], parsed["leverage"], parsed["tf"])

    # Pack and execute
    exec_sig = ExecSignal(**parsed)
    try:
        execute_signal(exec_sig)
    except Exception as e:
        log.error("[ERR] on_message: %s", e)

def start():
    if not DISCORD_TOKEN or not TARGET_CHANNEL_ID:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")
    client.run(DISCORD_TOKEN)
