import os
import discord

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal, is_symbol_allowed

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


def _to_exec(signal):
    return ExecSignal(
        symbol=signal.symbol,
        side=signal.side,
        entry_band=signal.entry_band,
        stop=signal.stop,
        tps=signal.take_profits,
    )


@client.event
async def on_ready():
    print(f"Logged in as {client.user} and listening to channel {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if message.channel.id != CHANNEL_ID:
        return

    txt = message.content or ""
    for e in message.embeds or []:
        if e.title:
            txt += f"\n{e.title}"
        if e.description:
            txt += f"\n{e.description}"
        for f in (e.fields or []):
            txt += f"\n{f.name}\n{f.value}"

    sig = parse_signal_from_text(txt)
    if not sig:
        return

    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not in allow list.")
        return

    res = execute_signal(_to_exec(sig))
    print(f"[EXEC] {sig.symbol} {sig.side} -> {res}")


def start():
    if not TOKEN or CHANNEL_ID == 0:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing.")
    client.run(TOKEN)
