# discord_listener.py
import asyncio
import logging
import os
import re
import discord

from parser import parse_signal
from execution import execute_signal

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")
AUTHOR_ALLOWLIST = [s.strip().lower() for s in (os.getenv("AUTHOR_ALLOWLIST", "") or "").split(",") if s.strip()]

HELLO = "👋 Ready. I’ll execute on allowed symbols."

intents = discord.Intents.default()
intents.message_content = True

class Bot(discord.Client):
    async def on_ready(self):
        log.info(f"[READY] Logged in as {self.user} | target CHANNEL_ID={CHANNEL_ID}")
        try:
            ch = await self.fetch_channel(CHANNEL_ID)
            await ch.send(HELLO)
            log.info("[READY] Sent hello message successfully.")
            log.info(f"[READY] Resolved channel: {ch.name} type={ch.type} parent_id={getattr(ch, 'category_id', None)}")
        except Exception as e:
            log.warning(f"[READY] Could not send hello: {e}")

    async def on_message(self, message: discord.Message):
        # Drop messages from ourselves
        if message.author == self.user:
            log.info("[DROP] our own message")
            return
        if AUTHOR_ALLOWLIST and message.author.name.lower() not in AUTHOR_ALLOWLIST:
            return

        try:
            mid = message.id
            content = message.content or ""
            log.info(
                f"[RX] msg_id={mid} author='{message.author.display_name}' "
                f"chan_id={message.channel.id} chan_name={getattr(message.channel,'name','?')} "
                f"type={message.channel.type} parent_id={getattr(message.channel, 'category_id', None)} "
                f"guild_id={getattr(message.guild,'id','?')} len={len(content)}"
            )
            sig = parse_signal(content)
            if not sig:
                log.info("[SKIP] Could not parse signal from message.")
                return

            log.info(
                f"[PASS] parsed: {sig.side} {sig.symbol} band=({sig.entry_low}, {sig.entry_high}) "
                f"{'SL=' + str(sig.stop_loss) if sig.stop_loss else ''} "
                f"TPn={len(sig.tps) if sig.tps else 0} "
                f"lev={sig.leverage or 0} TF={sig.timeframe or ''}"
            )
            execute_signal(sig)
        except Exception as e:
            log.exception(f"[ERR] on_message: {e}")

def start():
    if not TOKEN or CHANNEL_ID == 0:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing")
    client = Bot(intents=intents)
    log.info("logging in using static token")
    client.run(TOKEN)
