# discord_listener.py
import os
import asyncio
import logging
import discord

from parser import parse_signal_from_text
from execution import execute_signal, is_symbol_allowed

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Very talkative logs by default
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("signals-bot")

intents = discord.Intents.default()
# THIS IS CRITICAL: without it, on_message will not fire with the content
intents.message_content = True
intents.guilds = True
intents.messages = True

_client = discord.Client(intents=intents)

def _extract_text(msg: discord.Message) -> str:
    """Return plain text + any embed text so our parser can see everything."""
    parts = []
    if msg.content:
        parts.append(msg.content)

    for em in msg.embeds or []:
        if em.title:
            parts.append(str(em.title))
        if em.description:
            parts.append(str(em.description))
        if em.fields:
            for f in em.fields:
                # "Name: BTC/USD" style fields will be picked up
                parts.append(f"{f.name}: {f.value}")

        # Some bots put numbers in footer
        if em.footer and em.footer.text:
            parts.append(str(em.footer.text))

    return "\n".join(parts).strip()

@_client.event
async def on_ready():
    log.info("Logged in as %s and listening to channel %s", _client.user, CHANNEL_ID)

@_client.event
async def on_message(message: discord.Message):
    # Ignore ourselves
    if message.author == _client.user:
        return

    # Wrong channel? Log and skip silently
    if message.channel.id != CHANNEL_ID:
        return

    text = _extract_text(message)
    if not text:
        log.info("Message in channel %s had no text/embeds; skipping.", CHANNEL_ID)
        return

    log.info("Message received (%d chars).", len(text))
    sig = parse_signal_from_text(text)

    if not sig:
        log.info("Parse failed — message does not look like a VIP card.")
        return

    log.info(
        "Parsed signal: symbol=%s side=%s entry=%s stop=%.6f tps=%s lev=%s tf=%s",
        sig.symbol,
        sig.side,
        sig.entry_band,
        sig.stop,
        [round(x, 6) for x in sig.take_profits],
        sig.leverage,
        sig.timeframe,
    )

    if not is_symbol_allowed(sig.symbol):
        log.info("Symbol %s not in HYPER_ONLY_EXECUTE_SYMBOLS; skipping.", sig.symbol)
        return

    try:
        await execute_signal(sig)
        log.info("Execution submitted for %s.", sig.symbol)
    except Exception as e:
        log.exception("Execution error: %s", e)

def start():
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing.")
    _client.run(TOKEN)
