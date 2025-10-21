# discord_listener.py
"""
Discord ingestion for VIP signals.

- Listens to a single channel (DISCORD_CHANNEL_ID).
- Parses each message with parser.parse_signal_from_text.
- Applies allow-list (HYPER_ONLY_EXECUTE_SYMBOLS) if provided.
- Builds ExecSignal using entry_band and calls execute_signal(...).
"""

from __future__ import annotations

import os
import asyncio
import discord
from typing import Set

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal


# ---------- Env / allow list ----------

def _env_bool(key: str, default: bool = False) -> bool:
    return str(os.getenv(key, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or ""
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
ALLOW: Set[str] = {
    s.strip().upper()
    for s in os.getenv("HYPER_ONLY_EXECUTE_SYMBOLS", "").split(",")
    if s.strip()
}

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set.")
if CHANNEL_ID == 0:
    raise RuntimeError("DISCORD_CHANNEL_ID is not set or invalid.")


# ---------- Discord client ----------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        chan = await client.fetch_channel(CHANNEL_ID)
        cname = getattr(chan, "name", "?")
        parent = getattr(chan, "category_id", None) or getattr(chan, "parent_id", None)
        print(f"[READY] Logged in as {client.user} | target CHANNEL_ID={CHANNEL_ID}")
        print(f"[READY] Resolved channel: {cname} type={getattr(chan, 'type', None)} parent_id={parent}")
        # Say hello (ok if fails)
        try:
            await chan.send("👋 Bot online (debug). I can read this channel.")
            print("[READY] Sent hello message successfully.")
        except Exception as e:
            print(f"[READY] Could not send hello message: {e}")
    except Exception as e:
        print(f"[READY] Error resolving channel: {e}")


def _in_allow(symbol: str) -> bool:
    if not ALLOW:
        return True
    return symbol.upper() in ALLOW


@client.event
async def on_message(message: discord.Message):
    """
    Ingest new messages, parse, filter, and execute.
    """
    try:
        # Ignore our own messages
        if message.author == client.user or message.author.bot:
            return

        # Only the configured channel
        if message.channel.id != CHANNEL_ID:
            return

        content = message.content or ""
        author = str(message.author)
        chan_name = getattr(message.channel, "name", "?")
        print(
            f"[RX] msg_id={message.id} author='{author}' "
            f"chan_id={message.channel.id} chan_name={chan_name} "
            f"type={getattr(message.channel, 'type', None)} "
            f"parent_id={getattr(message.channel, 'category_id', None)} "
            f"guild_id={getattr(message.guild, 'id', None)} len={len(content)}"
        )

        sig = parse_signal_from_text(content)
        if not sig:
            print("[PARSE] no signal detected")
            return

        symbol = sig.symbol.upper()

        if not _in_allow(symbol):
            print(f"[SKIP] {symbol} not in allow-list")
            return

        # Build ExecSignal with entry_band (parser already gives us tuple(low, high))
        exec_sig = ExecSignal(
            symbol=sig.symbol,
            side=sig.side,
            entry_band=sig.entry_band,
            stop=sig.stop,
            tps=sig.take_profits,
            leverage=sig.leverage,
            timeframe=sig.timeframe,
        )

        print(f"[PASS] from '{author}' in channel: {content.splitlines()[0][:64]}...")
        try:
            await execute_signal(exec_sig)
            print(f"[EXEC] submitted {sig.side} {sig.symbol} {sig.entry_band} SL={sig.stop}")
        except Exception as e:
            print(f"[EXC] execution error: {e}")

    except Exception as e:
        print(f"[ERR] on_message crash: {e}")


# ---------- Entrypoint ----------

def start():
    client.run(TOKEN)


if __name__ == "__main__":
    start()
