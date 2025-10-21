# discord_listener.py
import os
import asyncio
import discord

from parser import parse_signal_from_text
from execution import execute_signal

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")

DEBUG = str(os.getenv("DEBUG", "")).lower() in ("1", "true", "yes", "on")

def _log(msg: str):
    if DEBUG:
        print(f"[listener] {msg}", flush=True)

def _extract_text_from_message(message: discord.Message) -> str:
    parts = []
    if message.content:
        parts.append(message.content)

    for emb in message.embeds or []:
        # Embed title/description
        if emb.title:
            parts.append(emb.title)
        if emb.description:
            parts.append(emb.description)

        # Embed fields
        for f in emb.fields or []:
            if f.name:
                parts.append(str(f.name))
            if f.value:
                parts.append(str(f.value))

        # Embed footer
        try:
            ft = getattr(emb.footer, "text", None)
            if ft:
                parts.append(ft)
        except Exception:
            pass

    text = "\n".join(p for p in parts if p)
    return text

class SignalClient(discord.Client):
    async def on_ready(self):
        print(
            f"Logged in as {self.user} and listening to channel {DISCORD_CHANNEL_ID}",
            flush=True,
        )

    async def on_message(self, message: discord.Message):
        # Channel filter
        if not DISCORD_CHANNEL_ID or message.channel.id != DISCORD_CHANNEL_ID:
            return

        # Ignore our own messages
        if message.author == self.user:
            return

        raw = _extract_text_from_message(message)
        if not raw.strip():
            _log("skip: message has no text content/embeds to parse")
            return

        sig = parse_signal_from_text(raw)
        if not sig:
            _log("skip: parser returned None")
            return

        _log(
            f"parsed: symbol={sig.symbol} side={sig.side} "
            f"entry={sig.entry_band} sl={sig.stop} tps={sig.take_profits[:3]}..."
        )

        try:
            await execute_signal(sig)
        except Exception as e:
            _log(f"execute error: {e}")

def run():
    intents = discord.Intents.default()
    intents.message_content = True  # make sure this is enabled in the Dev Portal too
    client = SignalClient(intents=intents)
    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    run()
