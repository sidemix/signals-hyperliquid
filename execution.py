import os
import asyncio
import discord

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal, is_symbol_allowed

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Make sure we can read message content
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


def _to_exec(signal):
    # Convert parsed model -> ExecSignal the executor expects
    return ExecSignal(
        symbol=signal.symbol,               # 'ETH/USD'
        side=signal.side,                   # 'LONG'|'SHORT'
        entry_band=signal.entry_band,       # (lo, hi)
        stop=signal.stop,
        tps=signal.take_profits
    )


@client.event
async def on_ready():
    print(f"Logged in as {client.user} and listening to channel {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message):
    # Ignore other channels / self
    if message.author == client.user:
        return
    if message.channel.id != CHANNEL_ID:
        return

    txt = message.content or ""
    # include embeds text too (your VIP cards are embeds)
    if message.embeds:
        for e in message.embeds:
            if e.description:
                txt += f"\n{e.description}"
            if e.title:
                txt += f"\n{e.title}"
            if e.fields:
                for f in e.fields:
                    txt += f"\n{f.name}\n{f.value}"

    sig = parse_signal_from_text(txt)
    if not sig:
        return

    # quick allow-list gate (accepts ETH/USD, ETH-USD, ETH)
    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not in allow list.")
        return

    res = execute_signal(_to_exec(sig))
    print(f"[EXEC] {sig.symbol} {sig.side} -> {res}")


def start():
    if not TOKEN or CHANNEL_ID == 0:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing.")
    client.run(TOKEN)
