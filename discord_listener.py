# discord_listener.py
from __future__ import annotations

import os
import re
import asyncio
import discord

from parser import parse_signal_from_text  # your parser returning Signal model
from execution import execute_signal, is_symbol_allowed


TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))


intents = discord.Intents.none()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        chan = await client.fetch_channel(CHANNEL_ID)
        # Some deployments need a try/except for send permission
        try:
            await chan.send("👋 Bot online (debug). I can read this channel.")
            print("[READY] Sent hello message successfully.")
        except Exception as e:
            print(f"[READY] Could not send hello message: {e}")

        kind = getattr(chan, "type", None)
        parent_id = getattr(chan, "category_id", None)
        print(
            f"[READY] Logged in as {client.user} | target CHANNEL_ID={CHANNEL_ID}"
        )
        print(
            f"[READY] Resolved channel: {getattr(chan, 'name', 'unknown')} "
            f"type={kind} parent_id={parent_id}"
        )
    except Exception as e:
        print(f"[READY] Failed channel resolve: {e}")


@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages
    if message.author.id == client.user.id:
        print("[RX] our own message -> drop")
        return

    # Only watch the specified channel
    if message.channel.id != CHANNEL_ID:
        return

    text = message.content or ""
    author = getattr(message.author, "name", "unknown")
    print(
        f"[RX] msg_id={message.id} author='{author}' "
        f"chan_id={message.channel.id} chan_name={getattr(message.channel, 'name', '?')} "
        f"len={len(text)}"
    )

    # Quick bypass if it's obviously not a VIP card (very light)
    if "VIP" not in text and "Entry Price" not in text and "StopLoss" not in text:
        return

    sig = parse_signal_from_text(text)
    if not sig:
        print("[SKIP] could not parse signal")
        return

    # Gate by allow list, if set
    if not is_symbol_allowed(sig.symbol):
        print("[SKIP] symbol not in HYPER_ONLY_EXECUTE_SYMBOLS")
        return

    try:
        print(f"[PASS] from '{author}' in channel: VIP Signal...")
        execute_signal(
            symbol=sig.symbol,
            side=sig.side,
            entry_band=sig.entry_band,
            stop=sig.stop,
            tps=sig.take_profits,
            leverage=sig.leverage,
            timeframe=sig.timeframe,
        )
    except Exception as e:
        print(f"[EXC] execution error: {e}")


def start():
    if not TOKEN or not CHANNEL_ID:
        raise RuntimeError("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set.")
    # discord.py wants an event loop in some environments
    try:
        client.run(TOKEN)
    except KeyboardInterrupt:
        pass
