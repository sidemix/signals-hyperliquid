# discord_listener.py
import os
import textwrap
import discord

from parser import parse_signal_from_text
from execution import ExecSignal, execute_signal, is_symbol_allowed

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _short(s: str, n: int = 300) -> str:
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def _collect_text(message: discord.Message) -> str:
    """Concatenate plain text + embed contents so the parser sees everything."""
    txt = message.content or ""

    # Include embed content (title/description/fields) if present
    for e in message.embeds or []:
        if e.title:
            txt += f"\n{e.title}"
        if e.description:
            txt += f"\n{e.description}"
        for f in getattr(e, "fields", []) or []:
            if f.name:
                txt += f"\n{f.name}"
            if f.value:
                txt += f"\n{f.value}"

    return txt.strip()


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
    print(f"Logged in as {client.user} and listening to channel {CHANNEL_ID}", flush=True)


@client.event
async def on_message(message: discord.Message):
    # Ignore ourselves
    if message.author == client.user:
        return

    # Accept messages posted directly in the configured channel
    in_channel = (message.channel.id == CHANNEL_ID)

    # …and also messages posted in THREADS whose parent is the configured channel
    parent_id = getattr(message.channel, "parent_id", None)
    in_thread_under_channel = (parent_id == CHANNEL_ID)

    if not (in_channel or in_thread_under_channel):
        return

    where = "channel" if in_channel else f"thread<{message.channel.id}> under {parent_id}"
    txt = _collect_text(message)

    print(
        f"[DISCORD] from '{getattr(message.author, 'name', 'unknown')}' in {where} "
        f"({len(txt)} chars): {_short(txt)}",
        flush=True,
    )

    sig = parse_signal_from_text(txt)
    if not sig:
        print("[PARSE] no match — message ignored.", flush=True)
        return

    # Before executing, double-check allowlist
    if not is_symbol_allowed(sig.symbol):
        print(f"[SKIP] {sig.symbol} not in HYPER_ONLY_EXECUTE_SYMBOLS.", flush=True)
        return

    # Execute
    res = execute_signal(_to_exec(sig))
    print(f"[EXEC] {sig.symbol} {sig.side} -> {res}", flush=True)


def start():
    if not TOKEN or CHANNEL_ID == 0:
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID missing.")
    client.run(TOKEN)
